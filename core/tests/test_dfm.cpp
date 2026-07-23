// core/tests/test_dfm.cpp — process profiles + FDM-default resolver (OCCT-free).
// Mirrors engine_oracle/dfm/test_process_profiles.py (the section-5 numbers, the
// ambiguity-gate rules, the fit math) plus a GOLDEN parity replay of
// tests/data/dfm.json (generated from the live oracle by
// core/tests/data/gen_r4e6_fixtures.py). The material-density defaults read the
// source-tree registry.json (CHUDWARE_TEST_REGISTRY), same as the oracle.
#define DOCTEST_CONFIG_IMPLEMENT
#include <doctest/doctest.h>

#include <cstdlib>
#include <fstream>

#include "dfm/process_profiles.h"

using namespace chudware::dfm;
using nlohmann::json;

int main(int argc, char** argv) {
#ifdef _WIN32
    _putenv_s("CHUDWARE_MATERIALS_REGISTRY", CHUDWARE_TEST_REGISTRY);
#else
    setenv("CHUDWARE_MATERIALS_REGISTRY", CHUDWARE_TEST_REGISTRY, 1);
#endif
    doctest::Context ctx(argc, argv);
    return ctx.run();
}

TEST_CASE("known processes + the section-5 profile numbers") {
    const auto kp = known_processes();
    CHECK(kp == std::vector<std::string>{"fdm", "cnc_mill", "lathe", "sheet_metal",
                                         "laser", "injection"});

    const json fdm = get_profile("fdm");
    CHECK(fdm["process"] == "fdm");
    CHECK(fdm["material"]["name"] == "PLA");
    CHECK(fdm["params"]["min_wall_mm"] == 1.0);
    CHECK(fdm["params"]["hole_comp_mm"] == doctest::Approx(0.20));
    CHECK(fdm["params"]["tolerance_band_mm"] == doctest::Approx(0.40));
    CHECK(fdm["params"]["fit_classes_mm"]["slip"] == doctest::Approx(0.40));
    CHECK(fdm["params"]["overhang"]["max_deg"] == 45);
    CHECK(fdm["params"]["thread"]["strategy"] == "heat_set_insert");

    const json inj = get_profile("injection");
    CHECK(inj["params"]["draft"]["required"] == true);
    CHECK(inj["params"]["draft"]["min_deg"] == doctest::Approx(1.0));

    CHECK_THROWS_WITH_AS(get_profile("lost_wax"),
                         "Unknown process 'lost_wax'. Known processes: "
                         "['fdm', 'cnc_mill', 'lathe', 'sheet_metal', 'laser', 'injection'].",
                         DfmError);
}

TEST_CASE("fit_clearance + modeled_dim (the fit brain)") {
    CHECK(fit_clearance("fdm", "slip") == doctest::Approx(0.40));
    CHECK(fit_clearance("cnc_mill", "press") == doctest::Approx(-0.02));
    CHECK_THROWS_WITH_AS(fit_clearance("sheet_metal", "slip"),
                         "Process 'sheet_metal' has no diametral 'slip' fit class "
                         "(it is a flat/sheet process; holes are cut to size).",
                         DfmError);
    CHECK_THROWS_WITH_AS(fit_clearance("fdm", "snug"),
                         "Unknown fit class 'snug'. Known: ['free', 'location', 'press', 'slip'].",
                         DfmError);

    const json hole = modeled_dim(10.0, "slip", "fdm", true);
    CHECK(hole["modeled_mm"] == doctest::Approx(10.60));  // 10 + 0.40 + 0.20 comp
    CHECK(hole["clearance_mm"] == doctest::Approx(0.40));
    CHECK(hole["hole_comp_mm"] == doctest::Approx(0.20));
    CHECK(hole["rationale"] ==
          "10 mm nominal, fdm slip clearance +0.40 mm, + printed-hole comp 0.20 mm");

    const json press = modeled_dim(12.0, "press", "cnc_mill");
    CHECK(press["modeled_mm"] == doctest::Approx(11.98));  // interference
}

TEST_CASE("resolve_process: the FDM-default ambiguity gate") {
    // Default prior: no cues -> FDM, no question.
    json r = resolve_process(json("a knob"));
    CHECK(r["chosen"] == "fdm");
    CHECK(r["confidence"] == doctest::Approx(0.86));
    CHECK(r["ambiguous"] == false);

    // Explicit keyword short-circuits.
    r = resolve_process(json("3d print a bracket"));
    CHECK(r["chosen"] == "fdm");
    CHECK(r["confidence"] == doctest::Approx(0.95));

    // Rule (c): explicit print + metal -> must_block.
    r = resolve_process(json("3d print this steel gear"));
    CHECK(r["must_block"] == true);
    CHECK(r["ambiguous"] == true);
    CHECK(r["clarification"]["suggested"] == "cnc_mill");

    // Metal pins a metal process; no cues -> CNC default.
    r = resolve_process(json("a steel widget"));
    CHECK(r["chosen"] == "cnc_mill");
    CHECK(r["confidence"] == doctest::Approx(0.9));

    // Rule (a): soft cue within margin of the FDM prior, no anchor -> ask.
    r = resolve_process(json("threaded flat plate, precision"));
    CHECK(r["ambiguous"] == true);
    CHECK(r["must_block"] == false);
    CHECK(r["clarification"]["options"] == json::parse(R"(["cnc_mill","fdm"])"));

    // Plastic reinforces the prior.
    r = resolve_process(json("plastic housing"));
    CHECK(r["chosen"] == "fdm");
    CHECK(r["confidence"] == doctest::Approx(0.9));
}

TEST_CASE("process_block_text: the planner PROCESS BLOCK") {
    const std::string block = process_block_text(get_profile("fdm"));
    CHECK(block.find("PROCESS BLOCK — manufacturing = FDM 3D PRINT / PLA") == 0);
    CHECK(block.find("slip +0.40") != std::string::npos);
    CHECK(block.find("heat set insert (printed threads only ≥ M5)") != std::string::npos);
    CHECK(block.find("Overhangs steeper than 45°") != std::string::npos);

    const std::string inj = process_block_text(get_profile("injection"));
    CHECK(inj.find("Draft REQUIRED ≥ 1°") != std::string::npos);
}

TEST_CASE("dispatch: ownership + envelope") {
    json result;
    std::string err;
    CHECK_FALSE(call("not_ours", json::object(), result, err));
    CHECK(call("dfm_known_processes", json::object(), result, err));
    CHECK(result["ok"] == true);
    CHECK(result["result"].size() == 6);
    CHECK(call("dfm_fit_clearance", json::parse(R"({"process":"fdm","fit_class":"snug"})"),
               result, err));
    CHECK(result["ok"] == false);
    CHECK_FALSE(result.contains("code"));
}

TEST_CASE("golden parity: every oracle fixture deep-equals (tests/data/dfm.json)") {
    std::ifstream f(std::string(CHUDWARE_TEST_DATA_DIR) + "/dfm.json");
    REQUIRE_MESSAGE(f.good(), "dfm.json golden not found at CHUDWARE_TEST_DATA_DIR");
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
