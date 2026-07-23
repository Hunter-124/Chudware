// core/tests/test_tolerance.cpp — 1D tolerance stack-up (OCCT-free). Mirrors the
// Python oracle's suite (engine_oracle/tolerance/test_stackup.py: the textbook
// three-dim gap chain, edge cases, error messages) plus a GOLDEN parity replay:
// every case in tests/data/tolerance.json was generated from the live oracle
// (core/tests/data/gen_r4e6_fixtures.py) at the MCP-envelope level and must deep-
// equal the C++ dispatch output exactly.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <fstream>

#include "tolerance/stackup.h"

using namespace chudware::tolerance;
using nlohmann::json;

TEST_CASE("textbook 3-dim chain (Fischer Ch. 3 hand-calc)") {
    const json dims = json::parse(R"([
        {"name": "A_slot_width",   "nominal": 40.000, "tol": 0.050, "direction":  1.0},
        {"name": "B_block1_width", "nominal": 17.000, "tol": 0.025, "direction": -1.0},
        {"name": "C_block2_width", "nominal": 20.000, "tol": 0.040, "direction": -1.0}
    ])");
    const json r = stackup_analyze(dims);
    CHECK(r["ok"] == true);
    CHECK(r["nominal_gap"] == doctest::Approx(3.000));
    CHECK(r["worst_case"]["total_tol"] == doctest::Approx(0.115));
    CHECK(r["worst_case"]["gap_max"] == doctest::Approx(3.115));
    CHECK(r["worst_case"]["gap_min"] == doctest::Approx(2.885));
    const double t_rss = std::sqrt(0.050 * 0.050 + 0.025 * 0.025 + 0.040 * 0.040);
    CHECK(r["rss"]["total_tol"] == doctest::Approx(t_rss));
    // Sensitivity ranking: A (0.050) > C (0.040) > B (0.025), stable.
    REQUIRE(r["contributors"].size() == 3);
    CHECK(r["contributors"][0]["name"] == "A_slot_width");
    CHECK(r["contributors"][1]["name"] == "C_block2_width");
    CHECK(r["contributors"][2]["name"] == "B_block1_width");
    CHECK(r["contributors"][0]["pct_wc"] == doctest::Approx(0.050 / 0.115 * 100).epsilon(1e-4));
    CHECK(r["assumed_dims"].empty());
    CHECK(r["disclaimer"].get<std::string>().find("Preliminary") != std::string::npos);
}

TEST_CASE("edge cases: single dim, zero tol, assumed flag, errors") {
    CHECK(stackup_analyze(json::parse(R"([{"name":"d","nominal":10.0,"tol":0.1}])"))
          ["rss"]["total_tol"] == doctest::Approx(0.1));

    const json assumed = stackup_analyze(json::parse(
        R"([{"name":"d1","nominal":5.0,"tol":0.05,"assumed":true},
            {"name":"d2","nominal":5.0,"tol":0.05}])"));
    REQUIRE(assumed["assumed_dims"].size() == 1);
    CHECK(assumed["assumed_dims"][0] == "d1");

    CHECK_THROWS_WITH_AS(stackup_analyze(json::array()),
                         "stackup_analyze requires at least one dimension", StackupError);
    CHECK_THROWS_WITH_AS(stackup_analyze(json::parse(R"([{"name":"d","nominal":5.0,"tol":-0.1}])")),
                         "Dimension 'd': 'tol' must be ≥ 0, got -0.1", StackupError);
    CHECK_THROWS_WITH_AS(stackup_analyze(json::parse(R"([{"name":"d","tol":0.1}])")),
                         "Dimension 'd': missing or invalid 'nominal'", StackupError);
    CHECK_THROWS_WITH_AS(stackup_analyze(json::parse(R"([{"name":"d","nominal":0.1}])")),
                         "Dimension 'd': missing or invalid 'tol'", StackupError);
}

TEST_CASE("dispatch: method ownership, mode scoping, error envelopes") {
    json result;
    std::string err;
    CHECK_FALSE(call("not_ours", json::object(), result, err));

    CHECK(call("stackup_analyze",
               json::parse(R"({"dims":[{"name":"d","nominal":1.0,"tol":0.1}],"mode":"worst_case"})"),
               result, err));
    CHECK(result.contains("worst_case"));
    CHECK_FALSE(result.contains("rss"));

    CHECK(call("stackup_analyze",
               json::parse(R"({"dims":[{"name":"d","nominal":1.0,"tol":0.1}],"mode":"rss"})"),
               result, err));
    CHECK(result.contains("rss"));
    CHECK_FALSE(result.contains("worst_case"));

    CHECK(call("stackup_analyze",
               json::parse(R"({"dims":[],"mode":"both"})"), result, err));
    CHECK(result["ok"] == false);
    CHECK(result["code"] == "invalid_input");

    CHECK(call("stackup_analyze",
               json::parse(R"({"dims":[{"name":"d","nominal":1.0,"tol":0.1}],"mode":"wc"})"),
               result, err));
    CHECK(result["ok"] == false);
    CHECK(result["code"] == "invalid_mode");
    CHECK(result["error"] == "Invalid mode 'wc'. Choose: ['both', 'rss', 'worst_case']");
}

TEST_CASE("golden parity: every oracle fixture deep-equals (tests/data/tolerance.json)") {
    std::ifstream f(std::string(CHUDWARE_TEST_DATA_DIR) + "/tolerance.json");
    REQUIRE_MESSAGE(f.good(), "tolerance.json golden not found at CHUDWARE_TEST_DATA_DIR");
    const json cases = json::parse(f);
    for (const json& c : cases) {
        CAPTURE(c["name"]);
        json result;
        std::string err;
        REQUIRE(call("stackup_analyze", c["params"], result, err));
        CHECK_MESSAGE(result == c["result"],
                      "parity diff\n  got: " << result.dump() << "\n  oracle: "
                                              << c["result"].dump());
    }
}
