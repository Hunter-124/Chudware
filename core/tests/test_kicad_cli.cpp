#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "ecad/kicad_cli.h"
#include "ecad/kicad_libs.h"
#include "ecad/session.h"
#include "util/subprocess.h"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <string>

namespace fs = std::filesystem;
using nlohmann::json;
using namespace chudware::ecad;

namespace {
struct Fixture {
    fs::path dir = fs::temp_directory_path() /
        ("chudware-kicad-cli-test-" + std::to_string(
            std::chrono::steady_clock::now().time_since_epoch().count()));
    Fixture() { fs::create_directories(dir); }
    ~Fixture() { std::error_code ec; fs::remove_all(dir, ec); }
    fs::path copy_project(const fs::path& source) {
        const auto target = dir / source.filename();
        fs::copy(source, target, fs::copy_options::recursive);
        return target;
    }
};

std::string contents(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    REQUIRE(in.good());
    return std::string(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
}

bool available(const fs::path& demo) {
    if (!kicad_libs::detect().found || !fs::exists(demo)) {
        MESSAGE("KiCad CLI or installed KiCad demo unavailable; skipping integration case");
        return false;
    }
    return true;
}

struct SessionGuard {
    std::string id;
    explicit SessionGuard(const fs::path& path)
        : id(ecad_open(path.string()).at("doc_id").get<std::string>()) {}
    ~SessionGuard() { if (!id.empty()) ecad_close(id); }
};
}  // namespace

TEST_CASE("KiCad status identifies the installed CLI") {
    const json status = ecad_kicad_status();
    REQUIRE(status.contains("found"));
    if (!status["found"].get<bool>()) return;
    CHECK(status["version"].get<std::string>().rfind("10.", 0) == 0);
    CHECK_FALSE(status["cli"].get<std::string>().empty());
    CHECK(status["libraries"]["symbol"].is_number());
    CHECK(status["libraries"]["footprint"].is_number());
}

TEST_CASE("KiCad DRC parses violations and converts positions to document coordinates") {
    const fs::path demo = "/usr/share/kicad/demos/ecc83";
    if (!available(demo / "ecc83-pp.kicad_pcb")) return;
    Fixture fixture;
    const auto project = fixture.copy_project(demo);
    const auto board = project / "ecc83-pp.kicad_pcb";
    const auto before = contents(board);
    SessionGuard session(board);
    const json checked = ecad_kicad_check(session.id);
    CHECK(checked["kind"] == "drc");
    REQUIRE(checked["violations"].is_array());
    REQUIRE_FALSE(checked["violations"].empty());
    CHECK(checked["parity"].is_array());
    CHECK(checked["unconnected"].is_array());

    const auto report = fixture.dir / "native-drc.json";
    const auto process = chudware::util::run_process({kicad_libs::detect().cli, "pcb", "drc",
        "--refill-zones", "--schematic-parity", "--format", "json", "--units", "mm", "-o",
        report.string(), board.string()});
    REQUIRE(process.error.empty());
    REQUIRE(process.exit_code == 0);
    const json native = json::parse(contents(report));
    bool compared = false;
    for (const auto& violation : native["violations"]) {
        for (const auto& item : violation["items"]) {
            if (!item.contains("pos") || !item["pos"].contains("x") || !item["pos"].contains("y")) continue;
            for (const auto& mapped : checked["violations"]) {
                if (mapped["type"] != violation["type"]) continue;
                for (const auto& mapped_item : mapped["items"]) {
                    if (mapped_item["uuid"] != item["uuid"]) continue;
                    REQUIRE(mapped_item["xy"].is_array());
                    CHECK(mapped_item["xy"][0].get<double>() == doctest::Approx(item["pos"]["x"].get<double>()));
                    CHECK(mapped_item["xy"][1].get<double>() == doctest::Approx(-item["pos"]["y"].get<double>()));
                    compared = true;
                    break;
                }
                if (compared) break;
            }
            if (compared) break;
        }
        if (compared) break;
    }
    CHECK(compared);
    CHECK(contents(board) == before);
}

TEST_CASE("dirty PCB checks use unsaved project design rules") {
    const fs::path demo = "/usr/share/kicad/demos/ecc83";
    if (!available(demo / "ecc83-pp.kicad_pcb")) return;
    Fixture fixture;
    const auto project = fixture.copy_project(demo);
    const auto board = project / "ecc83-pp.kicad_pcb";
    const auto project_file = project / "ecc83-pp.kicad_pro";
    const auto board_before = contents(board);
    const auto project_before = contents(project_file);
    SessionGuard session(board);
    const long revision = session_file_info(session.id, false).at("revision").get<long>();
    ecad_apply_ops(session.id, revision,
                   json::array({{{"op", "pcb_set_rules"}, {"patch", {{"track_width_min", 2.0}}}}}));
    const auto report = ecad_kicad_check(session.id);
    bool narrow_tracks = false;
    for (const auto& violation : report["violations"])
        if (violation.value("type", std::string()) == "track_width") narrow_tracks = true;
    CHECK(narrow_tracks);
    CHECK(contents(board) == board_before);
    CHECK(contents(project_file) == project_before);
}

TEST_CASE("KiCad ERC reports violations from a sallen-key project") {
    const fs::path demo = "/usr/share/kicad/demos/simulation/sallen_key";
    if (!available(demo / "sallen_key.kicad_sch")) return;
    Fixture fixture;
    const auto project = fixture.copy_project(demo);
    // A broken project library is a real ERC issue, not a fabricated check result.
    fs::remove(project / "sallen_key_schlib.kicad_sym");
    SessionGuard session(project / "sallen_key.kicad_sch");
    const json checked = ecad_kicad_check(session.id);
    CHECK(checked["kind"] == "erc");
    REQUIRE_FALSE(checked["violations"].empty());
    CHECK(checked["counts"]["warning"].get<int>() > 0);
    const auto& first = checked["violations"][0];
    CHECK(first["description"].is_string());
    CHECK(first["type"].is_string());
    REQUIRE_FALSE(first["items"].empty());
    REQUIRE(first["items"][0]["xy"].is_array());
    CHECK(first["items"][0]["xy"][1].get<double>() < 0.0);
}

TEST_CASE("KiCad exports Gerbers and a SPICE deck without implicit project writes") {
    const fs::path ecc = "/usr/share/kicad/demos/ecc83";
    const fs::path sallen = "/usr/share/kicad/demos/simulation/sallen_key";
    if (!available(ecc / "ecc83-pp.kicad_pcb") || !available(sallen / "sallen_key.kicad_sch")) return;
    Fixture fixture;
    const auto board_project = fixture.copy_project(ecc);
    const auto sch_project = fixture.copy_project(sallen);
    SessionGuard board(board_project / "ecc83-pp.kicad_pcb");
    SessionGuard schematic(sch_project / "sallen_key.kicad_sch");
    const auto output = fixture.dir / "gerbers";
    const json gerber = ecad_export({{"doc_id", board.id}, {"kind", "gerber"}, {"path", output.string()}});
    REQUIRE(gerber["paths"].is_array());
    REQUIRE_FALSE(gerber["paths"].empty());
    bool copper = false;
    for (const auto& path : gerber["paths"]) {
        const fs::path file = path.get<std::string>();
        CHECK(fs::is_regular_file(file));
        if (file.extension() == ".gbr" || file.extension() == ".gtl") copper = true;
    }
    CHECK(copper);
    const auto before = contents(sch_project / "sallen_key.kicad_sch");
    const json spice = ecad_export({{"doc_id", schematic.id}, {"kind", "spice"}});
    CHECK(spice["paths"].empty());
    REQUIRE(spice["files"].size() == 1);
    CHECK(spice["files"][0]["content"].get<std::string>().find(".end") != std::string::npos);
    CHECK(spice["files"][0]["content"].get<std::string>().find(
              (sch_project / "ad8051.lib").generic_u8string()) != std::string::npos);
    CHECK(contents(sch_project / "sallen_key.kicad_sch") == before);
}

TEST_CASE("dirty KiCad copies preserve project-relative libraries and simulation models") {
    const fs::path demo = "/usr/share/kicad/demos/simulation/sallen_key";
    if (!available(demo / "sallen_key.kicad_sch")) return;
    Fixture fixture;
    const auto project = fixture.copy_project(demo);
    const auto schematic = project / "sallen_key.kicad_sch";
    const auto before = contents(schematic);
    SessionGuard session(schematic);
    const auto revision = session_file_info(session.id).at("revision").get<long>();
    ecad_apply_ops(session.id, revision,
                   json::array({{{"op", "sch_add_text"}, {"pos", json::array({0, 0})},
                                 {"text", "temporary session edit"}}}));
    REQUIRE(session_file_info(session.id)["dirty"] == true);
    const json checked = ecad_kicad_check(session.id);
    for (const auto& v : checked["violations"])
        CHECK(v.value("description", std::string()).find("sallen_key_schlib' was not found") == std::string::npos);
    const json spice = ecad_export({{"doc_id", session.id}, {"kind", "spice"}});
    REQUIRE(spice["files"].size() == 1);
    CHECK(spice["files"][0]["content"].get<std::string>().find(".end") != std::string::npos);
    CHECK(spice["files"][0]["content"].get<std::string>().find(
              (project / "ad8051.lib").generic_u8string()) != std::string::npos);
    CHECK(contents(schematic) == before);
}

TEST_CASE("generated STEP requires a bound KiCad board") {
    const auto created = ecad_new("pcb", "Unsaved");
    const auto id = created.at("doc_id").get<std::string>();
    try {
        ecad_kicad_step(id, json::object());
        FAIL("unbound board was accepted");
    } catch (const OpError& error) {
        CHECK(error.code == "needs_path");
    }
    ecad_close(id);
}

TEST_CASE("board STEP defaults beside the bound KiCad board") {
    if (!kicad_libs::detect().found) return;
    Fixture fixture;
    const auto board = fixture.dir / "physical.kicad_pcb";
    const auto created = ecad_new("pcb", "physical", board.string());
    const auto id = created.at("doc_id").get<std::string>();
    const json outline = {{{"type", "line"}, {"p1", {0, 0}}, {"p2", {20, 0}}},
                          {{"type", "line"}, {"p1", {20, 0}}, {"p2", {20, 20}}},
                          {{"type", "line"}, {"p1", {20, 20}}, {"p2", {0, 20}}},
                          {{"type", "line"}, {"p1", {0, 20}}, {"p2", {0, 0}}}};
    ecad_apply_ops(id, created.at("revision").get<long>(),
                   json::array({{{"op", "pcb_set_outline"}, {"closed", true}, {"shapes", outline}}}));
    ecad_save(id, "");
    const auto result = ecad_kicad_step(id, json::object());
    const fs::path expected = fixture.dir / "physical.step";
    CHECK(fs::path(result.at("step_path").get<std::string>()) == fs::absolute(expected));
    REQUIRE(fs::is_regular_file(expected));
    CHECK(contents(expected).find("ISO-10303-21") != std::string::npos);
    const auto custom = fixture.dir / "custom.step";
    const auto selected = ecad_kicad_step(id, json{{"path", custom.string()}});
    CHECK(fs::path(selected.at("step_path").get<std::string>()) == fs::absolute(custom));
    CHECK(fs::is_regular_file(custom));
    ecad_close(id);
}

TEST_CASE("KiCad output refuses an existing destination symlink") {
    const fs::path demo = "/usr/share/kicad/demos/simulation/sallen_key";
    if (!available(demo / "sallen_key.kicad_sch")) return;
    Fixture fixture;
    const auto project = fixture.copy_project(demo);
    SessionGuard session(project / "sallen_key.kicad_sch");
    const auto marker = fixture.dir / "protected";
    { std::ofstream file(marker); file << "not an export"; }
    const auto target = fixture.dir / "output.net";
    std::error_code error;
    fs::create_symlink(marker, target, error);
    if (error) {
        MESSAGE("Symbolic-link creation unavailable: ", error.message());
        return;
    }
    CHECK_THROWS_AS(ecad_export({{"doc_id", session.id}, {"kind", "netlist"},
                                 {"path", target.string()}}), OpError);
    CHECK(contents(marker) == "not an export");
}
