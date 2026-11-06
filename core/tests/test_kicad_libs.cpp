#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "ecad/kicad_libs.h"
#include "ecad/library.h"
#include "ecad/netlist.h"
#include "ecad/ops.h"
#include "ecad/schema.h"
#include "ecad/symlib.h"

#include <doctest/doctest.h>
#include <nlohmann/json.hpp>

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>

namespace fs = std::filesystem;
using nlohmann::json;
namespace libs = chudware::ecad::kicad_libs;

namespace {
void set_env(const char* key, const std::string& value) {
#ifdef _WIN32
    _putenv_s(key, value.c_str());
#else
    setenv(key, value.c_str(), 1);
#endif
}
void unset_env(const char* key) {
#ifdef _WIN32
    _putenv_s(key, "");
#else
    unsetenv(key);
#endif
}
struct Env {
    std::string key, previous;
    bool present;
    Env(const char* name, const std::string& value) : key(name), present(std::getenv(name) != nullptr) {
        if (present) previous = std::getenv(name);
        set_env(name, value);
    }
    ~Env() { if (present) set_env(key.c_str(), previous); else unset_env(key.c_str()); }
};
struct TempDir {
    fs::path path = fs::temp_directory_path() /
        ("chudware-kicad-libs-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    TempDir() { fs::create_directories(path); }
    ~TempDir() { libs::set_project_dir(""); std::error_code ec; fs::remove_all(path, ec); }
};
void put(const fs::path& path, const std::string& text) {
    fs::create_directories(path.parent_path());
    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    out << text;
}
int count_tag(const json& node, const char* tag) {
    if (!node.is_array()) return 0;
    int count = !node.empty() && node[0] == tag ? 1 : 0;
    for (const auto& child : node) if (child.is_array()) count += count_tag(child, tag);
    return count;
}
const json* find_tag(const json& node, const char* tag) {
    if (!node.is_array()) return nullptr;
    for (const auto& child : node)
        if (child.is_array() && !child.empty() && child[0] == tag) return &child;
    return nullptr;
}
}  // namespace

TEST_CASE("installed KiCad libraries resolve and search actual parts") {
    if (!fs::exists("/usr/share/kicad") || !fs::exists("/usr/bin/kicad-cli")) return;
    TempDir temp;
    Env config("XDG_CONFIG_HOME", (temp.path / "config").string());
    Env cli("CHUDWARE_KICAD_CLI", "/usr/bin/kicad-cli");
    Env root("CHUDWARE_KICAD_DIR", "/usr");
    Env symbols("KICAD10_SYMBOL_DIR", "/usr/share/kicad/symbols");
    Env footprints("KICAD10_FOOTPRINT_DIR", "/usr/share/kicad/footprints");
    Env templates("KICAD10_TEMPLATE_DIR", "/usr/share/kicad/template");
    libs::set_project_dir("");
    const auto install = libs::detect();
    REQUIRE(install.found);
    CHECK(install.version.rfind("10.", 0) == 0);
    CHECK(install.cli == "/usr/bin/kicad-cli");
    CHECK(install.symbol_dir == "/usr/share/kicad/symbols");
    const auto resistor = libs::symbol_sexpr("Device:R");
    REQUIRE(resistor.has_value());
    CHECK((*resistor)[0] == "symbol");
    CHECK((*resistor)[1] == "R");
    CHECK(count_tag(*resistor, "pin") == 2);
    const auto footprint = libs::footprint_sexpr("Resistor_SMD:R_0603_1608Metric");
    REQUIRE(footprint.has_value());
    CHECK((*footprint)[0] == "footprint");
    CHECK(count_tag(*footprint, "pad") == 2);
    CHECK(chudware::ecad::symlib::resolve_symbol("Device:R")["pins"].size() == 2);
    const json native_fp = chudware::ecad::library::resolve_footprint(
        "Resistor_SMD:R_0603_1608Metric");
    CHECK(native_fp["pads"].size() == 2);
    CHECK(native_fp["pads"][0]["size"] == json::array({0.8, 0.95}));
    CHECK(native_fp["model_3d"] ==
          "${KICAD10_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0603_1608Metric.step");
    CHECK(native_fp["model_3d_transform"]["scale"] == json::array({1.0, 1.0, 1.0}));
    bool has_courtyard = false;
    for (const json& graphic : native_fp["graphics"])
        if (graphic.value("layer", std::string()) == "F.CrtYd") has_courtyard = true;
    CHECK(has_courtyard);
    const json schematic = chudware::ecad::apply_ops(
        chudware::ecad::schema::new_schematic("simulation defaults"),
        json::array({json{{"op", "sch_add_symbol"}, {"libid", "Device:D"},
                          {"pos", json::array({0, 0})},
                          {"fields", json{{"Sim.Params", "is=2n"}}}}}))["doc"];
    REQUIRE(schematic["symbols"].size() == 1);
    CHECK(schematic["symbols"][0]["fields"]["Sim.Device"] == "D");
    CHECK(schematic["symbols"][0]["fields"]["Sim.Pins"] == "1=K 2=A");
    CHECK(schematic["symbols"][0]["fields"]["Sim.Params"] == "is=2n");
    const json netlist = chudware::ecad::build_netlist(schematic);
    REQUIRE(netlist["components"].size() == 1);
    CHECK(netlist["components"][0]["fields"]["Sim.Device"] == "D");
    CHECK_FALSE(libs::search_symbols("opamp", 10).empty());
    const auto exact = libs::search_symbols("Device:R", 10);
    REQUIRE_FALSE(exact.empty());
    CHECK(exact[0]["libid"] == "Device:R");
    {
        Env override_dir("KICAD10_SYMBOL_DIR", temp.path.string());
        CHECK(libs::detect().symbol_dir == temp.path.string());
    }
    std::error_code symlink_error;
    const fs::path alternate_cli = temp.path / "custom-kicad-cli";
    fs::create_symlink("/usr/bin/kicad-cli", alternate_cli, symlink_error);
    if (!symlink_error) {
        Env cli_override("CHUDWARE_KICAD_CLI", alternate_cli.string());
        const auto custom = libs::detect();
        CHECK(custom.found);
        CHECK(custom.cli == alternate_cli.string());
    }
}

TEST_CASE("project library tables override globals and derived symbols flatten") {
    TempDir temp;
    const fs::path project = temp.path / "project";
    const fs::path templates = temp.path / "template";
    const fs::path global = temp.path / "global";
    Env config("XDG_CONFIG_HOME", (temp.path / "config").string());
    Env appdata("APPDATA", (temp.path / "appdata").string());
    Env home("HOME", (temp.path / "home").string());
    Env symbol_dir("KICAD10_SYMBOL_DIR", global.string());
    Env template_dir("KICAD10_TEMPLATE_DIR", templates.string());
    put(templates / "sym-lib-table", "(sym_lib_table (lib (name Demo) (type KiCad) (uri \"${KICAD10_SYMBOL_DIR}/global.kicad_sym\")))");
    put(templates / "fp-lib-table", "(fp_lib_table)");
    put(global / "global.kicad_sym", "(kicad_symbol_lib (symbol \"Base\" (property \"Description\" \"global\")))");
    put(project / "sym-lib-table", "(sym_lib_table (lib (name Demo) (type KiCad) (uri \"${KIPRJMOD}/local.kicad_sym\")))");
    put(project / "fp-lib-table", "(fp_lib_table (lib (name Board) (type KiCad) (uri \"$(KIPRJMOD)/Board.pretty\")))");
    put(project / "local.kicad_sym", R"((kicad_symbol_lib
      (symbol "Base" (property "Reference" "U") (property "Description" "parent opamp")
        (property "ki_keywords" "precision analog")
        (symbol "Base_1_1" (pin passive line (name "A") (number "1"))
                           (pin passive line (name "B") (number "2"))))
      (symbol "Derived" (extends "Base") (property "Value" "Derived"))
    ))");
    const fs::path mod = project / "Board.pretty" / "Part.kicad_mod";
    put(mod, "(footprint \"Part\" (descr \"precision part\") (tags \"analog\") (pad \"1\" smd rect) (pad \"2\" smd rect))");
    libs::set_project_dir(project.string());
    const auto node = libs::symbol_sexpr("Demo:Derived");
    REQUIRE(node.has_value());
    CHECK((*node)[1] == "Derived");
    CHECK(find_tag(*node, "extends") == nullptr);
    CHECK(count_tag(*node, "pin") == 2);
    const json* unit = find_tag(*node, "symbol");
    REQUIRE(unit != nullptr);
    CHECK((*unit)[1] == "Derived_1_1");
    const auto hits = libs::search_symbols("precision", 10);
    REQUIRE(hits.size() == 2);
    CHECK(hits[0]["pin_count"] == 2);
    const auto found = libs::search_footprints("analog", 10);
    REQUIRE(found.size() == 1);
    CHECK(found[0]["fpid"] == "Board:Part");
    CHECK(found[0]["pad_count"] == 2);
    const auto all = libs::libraries();
    bool project_scope = false;
    for (const auto& lib : all["symbol"])
        if (lib["nickname"] == "Demo") project_scope = lib["scope"] == "project";
    CHECK(project_scope);
    const fs::path symbol_file = project / "local.kicad_sym";
    put(symbol_file, "(kicad_symbol_lib (symbol \"Revised\" "
                     "(property \"Description\" \"changed symbol\")))");
    fs::last_write_time(symbol_file, fs::last_write_time(symbol_file) + std::chrono::seconds(2));
    const auto changed_symbols = libs::search_symbols("changed symbol", 10);
    REQUIRE(changed_symbols.size() == 1);
    CHECK(changed_symbols[0]["libid"] == "Demo:Revised");
    CHECK_FALSE(libs::symbol_sexpr("Demo:Derived").has_value());
    put(mod, "(footprint \"Part\" (descr \"revised\") (pad \"1\" smd rect))");
    fs::last_write_time(mod, fs::last_write_time(mod) + std::chrono::seconds(2));
    const auto revised = libs::search_footprints("revised", 10);
    REQUIRE(revised.size() == 1);
    CHECK(revised[0]["pad_count"] == 1);
    libs::set_project_dir("");
    const auto global_base = libs::search_symbols("global", 10);
    REQUIRE(global_base.size() == 1);
    CHECK(global_base[0]["libid"] == "Demo:Base");
}
