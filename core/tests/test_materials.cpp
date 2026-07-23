// core/tests/test_materials.cpp — unified material registry (OCCT-free). Mirrors
// engine_oracle/materials/test_materials_registry.py (alias resolution, the FEA
// legacy-shape seam, the CAM fold-in seam) plus a GOLDEN parity replay of
// tests/data/materials.json (generated from the live oracle by
// core/tests/data/gen_r4e6_fixtures.py). The registry read is pointed at the
// source-tree engine_oracle/materials/registry.json (CHUDWARE_TEST_REGISTRY) — the
// SAME file the Python oracle reads — so parity is over the identical seed.
#define DOCTEST_CONFIG_IMPLEMENT
#include <doctest/doctest.h>

#include <cstdlib>
#include <fstream>

#include "materials/materials.h"

using namespace chudware::materials;
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

TEST_CASE("registry loads and resolves aliases (case/whitespace-insensitive)") {
    REQUIRE(available());
    CHECK(resolve_key("6061") == "aluminum_6061_t6");
    CHECK(resolve_key(" Aluminium 6061-T6 ") == resolve_key("6061"));  // canonical name
    CHECK(resolve_key("6061-T6") == resolve_key("6061"));              // alias
    CHECK(resolve_key("unobtanium-9000").empty());
    CHECK(resolve_key("").empty());

    const json rec = get("6061");
    REQUIRE(rec.is_object());
    CHECK(rec["key"] == resolve_key("6061"));
    CHECK(rec.contains("mechanical"));
    CHECK(rec["mechanical"].contains("E_mpa"));

    CHECK(get("unobtanium-9000").is_null());
    CHECK(density("pla").is_number());
    CHECK(density("unobtanium-9000").is_null());
}

TEST_CASE("FEA seam: legacy _MATERIALS / _MATERIAL_ALIASES shapes") {
    const json table = fea_table();
    REQUIRE(table.is_object());
    REQUIRE(table.contains("steel"));  // the legacy friendly default
    const json row = table["steel"];
    for (const char* f : {"name", "E_mpa", "nu", "density_g_cm3", "yield_mpa",
                          "ultimate_mpa", "endurance_mpa"})
        CHECK(row.contains(f));
    const json aliases = fea_aliases();
    CHECK(aliases.is_object());
}

TEST_CASE("CAM seam: fold-in data + CuttingDB group id") {
    const json cam = cam_data("6061");
    REQUIRE(cam.is_object());
    CHECK(cam.contains("id"));
    CHECK(cam_material_id("6061") == cam["id"].get<std::string>());
    CHECK(cam_data("pla").is_null());  // registry material without a cam section
    CHECK(cam_material_id("pla").empty());
    CHECK(cam_material_id("unobtanium-9000").empty());
    const json all = cam_materials();
    REQUIRE(all.is_object());
    CHECK(all.contains("aluminum_6061_t6"));
    for (auto it = all.begin(); it != all.end(); ++it) CHECK(it->contains("id"));
}

TEST_CASE("dispatch: ownership + envelope") {
    json result;
    std::string err;
    CHECK_FALSE(call("not_ours", json::object(), result, err));
    CHECK(call("material_density", json::parse(R"({"name":"pla"})"), result, err));
    CHECK(result["ok"] == true);
    CHECK(result["result"].is_number());
    CHECK(call("material_get", json::parse(R"({"name":"unobtanium-9000"})"), result, err));
    CHECK(result["result"].is_null());  // Python None -> JSON null
}

TEST_CASE("golden parity: every oracle fixture deep-equals (tests/data/materials.json)") {
    std::ifstream f(std::string(CHUDWARE_TEST_DATA_DIR) + "/materials.json");
    REQUIRE_MESSAGE(f.good(), "materials.json golden not found at CHUDWARE_TEST_DATA_DIR");
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
