// core/tests/test_mechanical.cpp — AGMA gear check + ISO 281 bearing life
// (OCCT-free). Mirrors engine_oracle/mechanical/test_gear_bearing.py (the Shigley
// worked examples, validation errors) plus a GOLDEN parity replay of
// tests/data/mechanical.json (generated from the live oracle by
// core/tests/data/gen_r4e6_fixtures.py at the MCP-envelope level).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <fstream>

#include "mechanical/mechanical.h"

using namespace chudware::mechanical;
using nlohmann::json;

TEST_CASE("gear_check: Shigley Ch. 14 worked example") {
    const json r = gear_check(json::parse(R"({
        "Wt": 1000, "module": 4, "face_width": 50, "pitch_diameter": 100,
        "J": 0.36, "I": 0.107, "Ko": 1.25, "Kv": 1.52, "Ks": 1.0, "Km": 1.8, "Cf": 1.0
    })"));
    CHECK(r["sigma_bending_MPa"] == doctest::Approx(47.5).epsilon(1e-3));
    CHECK(r["sigma_contact_MPa"] == doctest::Approx(483.0).epsilon(1e-3));
    CHECK(r["SF_bending"].is_null());
    CHECK(r["SF_contact"].is_null());
    CHECK(r["disclaimer"].get<std::string>().find("Preliminary") != std::string::npos);
}

TEST_CASE("gear_check: safety factors + validation") {
    const json r = gear_check(json::parse(R"({
        "Wt": 1000, "module": 4, "face_width": 50, "pitch_diameter": 100,
        "J": 0.36, "I": 0.107, "allowable_bending_stress": 200.0,
        "allowable_contact_stress": 1200.0
    })"));
    CHECK(r["SF_bending"].is_number());
    CHECK(r["SF_bending"].get<double>() > 1.0);
    CHECK(r["SF_contact"].is_number());

    CHECK_THROWS_WITH_AS(gear_check(json::parse(
                             R"({"Wt":500,"module":0,"face_width":25,"pitch_diameter":60,"J":0.4,"I":0.12})")),
                         "face_width, module, and pitch_diameter must be positive.", MechError);
    CHECK_THROWS_WITH_AS(gear_check(json::parse(
                             R"({"Wt":500,"module":2.5,"face_width":25,"pitch_diameter":60,"J":0,"I":0.12})")),
                         "Geometry factors J and I must be positive.", MechError);
    CHECK_THROWS_WITH_AS(gear_check(json::parse(
                             R"({"Wt":-5,"module":2.5,"face_width":25,"pitch_diameter":60,"J":0.4,"I":0.12})")),
                         "Tangential load Wt must be positive.", MechError);
}

TEST_CASE("bearing_life: Shigley Ch. 11 worked example + validation") {
    const json r = bearing_life(json::parse(R"({
        "C": 30000, "Fr": 8000, "Fa": 0, "n_rpm": 1200, "bearing_type": "ball"
    })"));
    CHECK(r["L10_million_rev"] == doctest::Approx(52.734).epsilon(1e-3));
    CHECK(r["L10h_hours"] == doctest::Approx(732.4).epsilon(1e-3));
    CHECK(r["P_equiv_N"] == doctest::Approx(8000.0));
    CHECK(r["p_exponent"] == doctest::Approx(3.0));

    const json roller = bearing_life(json::parse(
        R"({"C":50000,"Fr":10000,"Fa":2500,"n_rpm":900,"bearing_type":"roller",
            "X":0.4,"Y":1.5,"bearing_designation":"NU205"})"));
    CHECK(roller["p_exponent"] == doctest::Approx(10.0 / 3.0));
    CHECK(roller["P_equiv_N"] == doctest::Approx(0.4 * 10000 + 1.5 * 2500));
    CHECK(roller["bearing_designation"] == "NU205");

    CHECK_THROWS_WITH_AS(bearing_life(json::parse(R"({"C":0,"Fr":8000})")),
                         "Dynamic load rating C must be positive. "
                         "Supply C from the bearing manufacturer's datasheet.",
                         MechError);
    CHECK_THROWS_WITH_AS(bearing_life(json::parse(R"({"C":30000,"Fr":0,"Fa":0})")),
                         "At least one of Fr (radial) or Fa (axial) must be positive.",
                         MechError);
    CHECK_THROWS_WITH_AS(
        bearing_life(json::parse(R"({"C":30000,"Fr":8000,"bearing_type":"needle"})")),
        "bearing_type must be 'ball' or 'roller', got 'needle'.", MechError);
    CHECK_THROWS_WITH_AS(bearing_life(json::parse(R"({"C":30000,"Fr":8000,"X":0,"Y":0})")),
                         "Equivalent bearing load P = X*Fr + Y*Fa must be positive.", MechError);
    CHECK_THROWS_WITH_AS(bearing_life(json::parse(R"({"C":30000,"Fr":8000,"n_rpm":0})")),
                         "Shaft speed n_rpm must be positive.", MechError);
}

TEST_CASE("dispatch: ownership + ok envelope") {
    json result;
    std::string err;
    CHECK_FALSE(call("not_ours", json::object(), result, err));
    CHECK(call("gear_check",
               json::parse(R"({"Wt":1000,"module":4,"face_width":50,"pitch_diameter":100,
                               "J":0.36,"I":0.107})"),
               result, err));
    CHECK(result["ok"] == true);
    CHECK(result.contains("sigma_bending_MPa"));
    // Oracle ValueError -> _err()'s {"ok":false,"error"} (no code).
    CHECK(call("bearing_life", json::parse(R"({"C":0,"Fr":8000})"), result, err));
    CHECK(result["ok"] == false);
    CHECK_FALSE(result.contains("code"));
}

TEST_CASE("golden parity: every oracle fixture deep-equals (tests/data/mechanical.json)") {
    std::ifstream f(std::string(CHUDWARE_TEST_DATA_DIR) + "/mechanical.json");
    REQUIRE_MESSAGE(f.good(), "mechanical.json golden not found at CHUDWARE_TEST_DATA_DIR");
    const json cases = json::parse(f);
    for (const json& c : cases) {
        CAPTURE(c["name"]);
        json result;
        std::string err;
        REQUIRE(call(c["method"].get<std::string>(), c["params"], result, err));
        CHECK_MESSAGE(result == c["result"],
                      "parity diff\n  got: " << result.dump() << "\n  oracle: "
                                              << c["result"].dump());
    }
}
