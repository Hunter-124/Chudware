#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>
#include <nlohmann/json.hpp>

#include "ecad/kicad.h"
#include "ecad/kicad_libs.h"
#include "ecad/ops.h"
#include "ecad/schema.h"
#include "ecad/session.h"
#include "util/subprocess.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <vector>

using nlohmann::json;
namespace fs = std::filesystem;
namespace ki = chudware::ecad::kicad;
namespace schema = chudware::ecad::schema;

namespace {
std::string read_file(const fs::path& path) {
    std::ifstream in(path, std::ios::binary);
    std::ostringstream text;
    text << in.rdbuf();
    return text.str();
}
void collect_uuids(const json& value, std::set<std::string>& out) {
    if (value.is_object()) {
        if (value.contains("uuid") && value["uuid"].is_string()) out.insert(value["uuid"].get<std::string>());
        for (const auto& item : value.items()) collect_uuids(item.value(), out);
    } else if (value.is_array()) {
        if (value.size() > 1 && value[0] == "uuid" && value[1].is_string()) out.insert(value[1].get<std::string>());
        for (const auto& item : value) collect_uuids(item, out);
    }
}
// Versions and generators intentionally change when upgrading to KiCad 10.
// Remove only UUIDs minted for formerly UUID-less objects, not imported IDs.
void normalize(json& value, const std::set<std::string>& minted) {
    if (value.is_object()) {
        if (value.contains("uuid") && value["uuid"].is_string() && minted.count(value["uuid"].get<std::string>())) value.erase("uuid");
        if (value.contains("kicad") && value["kicad"].is_object()) {
            value["kicad"].erase("version");
            value["kicad"].erase("generator");
            value["kicad"].erase("generator_version");
        }
        for (auto& item : value.items()) normalize(item.value(), minted);
    } else if (value.is_array()) {
        for (std::size_t i = value.size(); i > 0; --i) {
            auto& item = value[i - 1];
            if (item.is_array() && item.size() > 1 && item[0] == "uuid" && item[1].is_string() &&
                minted.count(item[1].get<std::string>())) value.erase(i - 1);
            else normalize(item, minted);
        }
    }
}
void roundtrip(const fs::path& path) {
    INFO(path.string());
    const bool schematic = path.extension() == ".kicad_sch";
    const std::string input = read_file(path);
    REQUIRE_FALSE(input.empty());
    json original = schematic ? ki::import_kicad_sch(input) : ki::import_kicad_pcb(input);
    std::set<std::string> before, after, minted;
    collect_uuids(original, before);
    ki::assign_uuids(original);
    collect_uuids(original, after);
    for (const auto& id : after) if (!before.count(id)) minted.insert(id);
    const std::string text = schematic ? ki::write_kicad_sch(original) : ki::write_kicad_pcb(original);
    json reimported = schematic ? ki::import_kicad_sch(text) : ki::import_kicad_pcb(text);
    ki::assign_uuids(reimported);
    const std::string second = schematic ? ki::write_kicad_sch(reimported) : ki::write_kicad_pcb(reimported);
    CHECK(text == second);
    normalize(original, minted);
    normalize(reimported, minted);
    CHECK(original == reimported);
}

struct OracleFixture {
    fs::path dir = fs::temp_directory_path() /
        ("chudware-kicad-oracle-" + std::to_string(
            std::chrono::steady_clock::now().time_since_epoch().count()));
    OracleFixture() { fs::create_directories(dir); }
    ~OracleFixture() { std::error_code ec; fs::remove_all(dir, ec); }
};

std::string tag(const json& node) {
    return node.is_array() && !node.empty() && node[0].is_string() ?
        node[0].get<std::string>() : "";
}

const json* node_child(const json& node, const char* name) {
    if (node.is_array()) for (const auto& item : node)
        if (tag(item) == name) return &item;
    return nullptr;
}

// Ignore export paths/timestamps and file-derived Sheetname/Sheetfile only.
// Compare the entire electrical component/library data and named net nodes.
json schematic_netlist(const fs::path& path) {
    const json root = ki::sexpr_parse(read_file(path));
    json result = json::object();
    for (const char* section : {"components", "libparts", "nets"}) {
        json entries = json::array();
        if (const json* list = node_child(root, section)) {
            for (std::size_t i = 1; i < list->size(); ++i) {
                json entry = (*list)[i];
                if (!entry.is_array()) continue;
                for (std::size_t k = entry.size(); k > 1; --k) {
                    const json& part = entry[k - 1];
                    if (std::string(section) == "nets" && tag(part) == "code") {
                        entry.erase(k - 1);
                        continue;
                    }
                    if (std::string(section) == "components" && tag(part) == "property") {
                        const json* name = node_child(part, "name");
                        if (name && name->size() > 1 &&
                            ((*name)[1] == "Sheetname" || (*name)[1] == "Sheetfile"))
                            entry.erase(k - 1);
                    }
                }
                if (std::string(section) == "nets") {
                    json nodes = json::array();
                    for (std::size_t k = entry.size(); k > 1; --k)
                        if (tag(entry[k - 1]) == "node") {
                            nodes.push_back(entry[k - 1]);
                            entry.erase(k - 1);
                        }
                    std::sort(nodes.begin(), nodes.end());
                    for (auto& node : nodes) entry.push_back(std::move(node));
                }
                entries.push_back(std::move(entry));
            }
        }
        std::sort(entries.begin(), entries.end());
        result[section] = std::move(entries);
    }
    return result;
}

std::vector<std::string> ipc_netlist(const fs::path& path) {
    std::vector<std::string> records;
    std::ifstream in(path, std::ios::binary);
    std::string line;
    while (std::getline(in, line)) {
        while (!line.empty() && (line.back() == '\r' || line.back() == ' ')) line.pop_back();
        if ((!line.empty() && line[0] == '3') || line.rfind("P  NNAM", 0) == 0)
            records.push_back(std::move(line));
    }
    std::sort(records.begin(), records.end());
    return records;
}

void add_violation_keys(const json& violations, std::vector<std::string>& keys) {
    if (!violations.is_array()) return;
    for (const auto& violation : violations) {
        std::vector<std::string> positions;
        if (violation.contains("items") && violation["items"].is_array())
            for (const auto& item : violation["items"]) {
                if (!item.contains("pos") || !item["pos"].is_object()) continue;
                const auto& pos = item["pos"];
                if (!pos.contains("x") || !pos["x"].is_number() ||
                    !pos.contains("y") || !pos["y"].is_number()) continue;
                positions.push_back(std::to_string(std::llround(pos["x"].get<double>() * 1000)) +
                                    "," + std::to_string(std::llround(pos["y"].get<double>() * 1000)));
            }
        std::sort(positions.begin(), positions.end());
        std::string key = violation.value("type", std::string());
        for (const auto& pos : positions) key += "|" + pos;
        keys.push_back(std::move(key));
    }
}

std::vector<std::string> violation_multiset(const fs::path& path) {
    const json report = json::parse(read_file(path));
    std::vector<std::string> keys;
    for (const char* name : {"violations", "unconnected_items", "schematic_parity"})
        if (report.contains(name)) add_violation_keys(report[name], keys);
    if (report.contains("sheets") && report["sheets"].is_array())
        for (const auto& sheet : report["sheets"])
            if (sheet.contains("violations")) add_violation_keys(sheet["violations"], keys);
    std::sort(keys.begin(), keys.end());
    return keys;
}

void run_cli(const std::vector<std::string>& args, const fs::path& home) {
    chudware::util::ProcessOptions options;
    options.timeout = std::chrono::seconds(120);
    options.env["HOME"] = home.u8string();
    options.env["XDG_CONFIG_HOME"] = (home / "config").u8string();
    options.env["XDG_CACHE_HOME"] = (home / "cache").u8string();
    const auto result = chudware::util::run_process(args, options);
    INFO(result.output, " ", result.error);
    REQUIRE(result.error.empty());
    REQUIRE_FALSE(result.timed_out);
    REQUIRE(result.exit_code == 0);
}

std::vector<fs::path> demo_files() {
    const fs::path root("/usr/share/kicad/demos");
    std::vector<fs::path> files{
        root / "simulation/sallen_key/sallen_key.kicad_sch",
        root / "ecc83/ecc83-pp.kicad_sch",
        root / "ecc83/ecc83-pp.kicad_pcb",
        root / "video/video.kicad_pcb"};
    for (const auto& dir : {root / "simulation/subsheets", root / "pic_programmer"}) {
        if (!fs::is_directory(dir)) continue;
        for (const auto& entry : fs::directory_iterator(dir)) {
            const auto ext = entry.path().extension();
            if ((dir.filename() == "subsheets" && ext == ".kicad_sch") ||
                (dir.filename() == "pic_programmer" && ext == ".kicad_pcb")) files.push_back(entry.path());
        }
    }
    std::sort(files.begin(), files.end());
    return files;
}

void compare_cli_oracle(const fs::path& demo, const std::string& cli) {
    OracleFixture fixture;
    const fs::path project = fixture.dir / "project";
    fs::copy(demo.parent_path(), project, fs::copy_options::recursive);
    const fs::path source = project / demo.filename();
    const bool schematic = demo.extension() == ".kicad_sch";
    const fs::path saved = project / ("resaved" + demo.extension().string());
    const auto opened = chudware::ecad::ecad_open(source.u8string());
    const std::string id = opened.at("doc_id").get<std::string>();
    struct CloseSession {
        std::string id;
        ~CloseSession() { chudware::ecad::ecad_close(id); }
    } close{id};
    chudware::ecad::ecad_save(id, saved.u8string());

    const fs::path home = fixture.dir / "home";
    fs::create_directories(home);
    auto check_report = [&](const fs::path& input, const fs::path& output) {
        run_cli({cli, schematic ? "sch" : "pcb", schematic ? "erc" : "drc",
                 "--format", "json", "--units", "mm", "-o", output.u8string(),
                 input.u8string()}, home);
    };
    const fs::path old_report = fixture.dir / "original.json";
    const fs::path new_report = fixture.dir / "resaved.json";
    check_report(source, old_report);
    check_report(saved, new_report);
    const auto before = violation_multiset(old_report);
    const auto after = violation_multiset(new_report);
    std::vector<std::string> removed, added;
    std::set_difference(before.begin(), before.end(), after.begin(), after.end(),
                        std::back_inserter(removed));
    std::set_difference(after.begin(), after.end(), before.begin(), before.end(),
                        std::back_inserter(added));
    INFO("Removed violations: ", json(removed).dump(), "; added: ", json(added).dump());
    CHECK(before == after);

    const fs::path old_netlist = fixture.dir / "original.net";
    const fs::path new_netlist = fixture.dir / "resaved.net";
    if (schematic) {
        for (const auto& pair : {std::make_pair(source, old_netlist), std::make_pair(saved, new_netlist)})
            run_cli({cli, "sch", "export", "netlist", "--format", "kicadsexpr",
                     "-o", pair.second.u8string(), pair.first.u8string()}, home);
        CHECK(schematic_netlist(old_netlist) == schematic_netlist(new_netlist));
    } else {
        for (const auto& pair : {std::make_pair(source, old_netlist), std::make_pair(saved, new_netlist)})
            run_cli({cli, "pcb", "export", "ipcd356", "-o",
                     pair.second.u8string(), pair.first.u8string()}, home);
        const auto old_records = ipc_netlist(old_netlist);
        CHECK_FALSE(old_records.empty());
        CHECK(old_records == ipc_netlist(new_netlist));
    }
}
}

TEST_CASE("KiCad demo files preserve modeled and passthrough content") {
    const fs::path root("/usr/share/kicad/demos");
    if (!fs::exists(root)) {
        MESSAGE("KiCad demos are not installed; skipping demo round trips");
        return;
    }
    for (const auto& file : demo_files()) {
        if (fs::exists(file)) roundtrip(file);
        else MESSAGE("KiCad demo not installed: ", file.string());
    }
}

TEST_CASE("KiCad CLI preserves demo violations and electrical netlists after save-as") {
    if (!fs::is_directory("/usr/share/kicad/demos")) {
        MESSAGE("KiCad demos are not installed; skipping KiCad CLI oracle");
        return;
    }
    const auto install = chudware::ecad::kicad_libs::detect();
    if (!install.found || install.cli.empty()) {
        MESSAGE("kicad-cli is not installed; skipping KiCad CLI oracle");
        return;
    }
    for (const auto& file : demo_files()) {
        if (!fs::exists(file)) {
            MESSAGE("KiCad demo not installed: ", file.string());
            continue;
        }
        INFO(file.string());
        compare_cli_oracle(file, install.cli);
    }
}

TEST_CASE("native operation documents write KiCad 10 schematic and board structures") {
    json sch = schema::new_schematic("Native");
    const json sch_ops = json::array({
        {{"op", "sch_add_symbol"}, {"libid", "Device:R"}, {"ref", "R1"}, {"value", "10k"}, {"pos", {0, 0}}},
        {{"op", "sch_add_symbol"}, {"libid", "Device:C"}, {"ref", "C1"}, {"value", "100n"}, {"pos", {10, 0}}},
        {{"op", "sch_add_wire"}, {"pts", {{0, 0}, {10, 0}}}},
        {{"op", "sch_add_label"}, {"text", "SIGNAL"}, {"scope", "global"}, {"pos", {0, 0}}},
        {{"op", "sch_add_power"}, {"net", "GND"}, {"pos", {10, 0}}}});
    sch = chudware::ecad::apply_ops(sch, sch_ops)["doc"];
    ki::assign_uuids(sch);
    const std::string sch_text = ki::write_kicad_sch(sch);
    CHECK(ki::sexpr_parse(sch_text)[0] == "kicad_sch");
    CHECK(sch_text.find("(version 20260306)") != std::string::npos);
    const json sch_again = ki::import_kicad_sch(sch_text);
    CHECK(sch_again["symbols"].size() == 2);
    CHECK(sch_again["wires"].size() == 1);
    CHECK(sch_again["power"].size() == 1);

    json board = schema::new_board("Native");
    const json pad = {{"id", "p1"}, {"number", "1"}, {"pos", {0, 0}}, {"angle", 0},
                      {"shape", "circle"}, {"size", {1.5, 1.5}}, {"type", "smd"},
                      {"layers", {"F.Cu", "F.Paste", "F.Mask"}}, {"drill", nullptr}, {"net", "GND"}};
    const json board_ops = json::array({
        {{"op", "pcb_set_outline"}, {"closed", true}, {"shapes", {
            {{"type", "line"}, {"p1", {0, 0}}, {"p2", {20, 0}}},
            {{"type", "line"}, {"p1", {20, 0}}, {"p2", {20, 20}}},
            {{"type", "line"}, {"p1", {20, 20}}, {"p2", {0, 20}}},
            {{"type", "line"}, {"p1", {0, 20}}, {"p2", {0, 0}}}}}},
        {{"op", "pcb_add_footprint"}, {"fpid", "Test:OnePad"}, {"ref", "U1"}, {"pos", {5, 5}}, {"pads", {pad}}},
        {{"op", "pcb_add_track"}, {"layer", "F.Cu"}, {"net", "GND"}, {"p1", {5, 5}}, {"p2", {10, 5}}},
        {{"op", "pcb_add_via"}, {"net", "GND"}, {"at", {10, 5}}},
        {{"op", "pcb_add_zone"}, {"net", "GND"}, {"layer", "F.Cu"}, {"polygon", {{1, 1}, {19, 1}, {19, 19}, {1, 19}}}}});
    board = chudware::ecad::apply_ops(board, board_ops)["doc"];
    ki::assign_uuids(board);
    const std::string pcb_text = ki::write_kicad_pcb(board);
    CHECK(ki::sexpr_parse(pcb_text)[0] == "kicad_pcb");
    CHECK(pcb_text.find("(version 20260206)") != std::string::npos);
    const json board_again = ki::import_kicad_pcb(pcb_text);
    CHECK(board_again["footprints"].size() == 1);
    CHECK(board_again["footprints"][0]["pads"][0]["net"] == "GND");
    CHECK(board_again["tracks"].size() == 1);
    CHECK(board_again["vias"].size() == 1);
    CHECK(board_again["zones"].size() == 1);
    const auto install = chudware::ecad::kicad_libs::detect();
    if (install.found && !install.cli.empty()) {
        OracleFixture fixture;
        const fs::path sch_file = fixture.dir / "native.kicad_sch";
        const fs::path pcb_file = fixture.dir / "native.kicad_pcb";
        { std::ofstream file(sch_file); file << sch_text; }
        { std::ofstream file(pcb_file); file << pcb_text; }
        run_cli({install.cli, "sch", "erc", "--format", "json", "-o",
                 (fixture.dir / "sch.json").u8string(), sch_file.u8string()}, fixture.dir);
        run_cli({install.cli, "pcb", "drc", "--format", "json", "-o",
                 (fixture.dir / "pcb.json").u8string(), pcb_file.u8string()}, fixture.dir);
    } else MESSAGE("kicad-cli is not installed; skipping native-file load checks");
}

TEST_CASE("native connected symbols have the same pin connectivity in KiCad") {
    const auto install = chudware::ecad::kicad_libs::detect();
    if (!install.found || install.cli.empty()) {
        MESSAGE("kicad-cli is not installed; skipping connected-symbol oracle");
        return;
    }
    json sch = schema::new_schematic("Divider");
    const json ops = json::array({
        {{"op", "sch_add_symbol"}, {"libid", "Simulation_SPICE:VDC"}, {"ref", "V1"}, {"pos", {0, 25.4}}},
        {{"op", "sch_add_symbol"}, {"libid", "Device:R"}, {"ref", "R1"}, {"pos", {25.4, 25.4}}, {"angle", 90}},
        {{"op", "sch_add_symbol"}, {"libid", "Device:R"}, {"ref", "R2"}, {"pos", {25.4, 0}}, {"angle", 90}},
        {{"op", "sch_connect"}, {"from", {{"sym", "s1"}, {"pin", "1"}}}, {"to", {{"sym", "s2"}, {"pin", "2"}}}},
        {{"op", "sch_connect"}, {"from", {{"sym", "s2"}, {"pin", "1"}}}, {"to", {{"sym", "s3"}, {"pin", "2"}}}},
        {{"op", "sch_connect"}, {"from", {{"sym", "s3"}, {"pin", "1"}}}, {"to", {{"sym", "s1"}, {"pin", "2"}}}}});
    sch = chudware::ecad::apply_ops(sch, ops)["doc"];
    ki::assign_uuids(sch);
    OracleFixture fixture;
    const fs::path path = fixture.dir / "divider.kicad_sch";
    const fs::path netlist_path = fixture.dir / "divider.net";
    { std::ofstream file(path); file << ki::write_kicad_sch(sch); }
    run_cli({install.cli, "sch", "export", "netlist", "--format", "kicadsexpr",
             "-o", netlist_path.u8string(), path.u8string()}, fixture.dir);
    const json netlist = ki::sexpr_parse(read_file(netlist_path));
    const json* nets = node_child(netlist, "nets");
    REQUIRE(nets);
    std::set<std::set<std::string>> connected;
    for (const auto& net : *nets) {
        if (tag(net) != "net") continue;
        std::set<std::string> pins;
        for (const auto& node : net) {
            if (tag(node) != "node") continue;
            const json* ref = node_child(node, "ref");
            const json* pin = node_child(node, "pin");
            if (ref && pin && ref->size() > 1 && pin->size() > 1)
                pins.insert((*ref)[1].get<std::string>() + "." + (*pin)[1].get<std::string>());
        }
        connected.insert(std::move(pins));
    }
    const std::set<std::set<std::string>> expected{
        {"R1.2", "V1.1"}, {"R1.1", "R2.2"}, {"R2.1", "V1.2"}};
    INFO("KiCad connected pin sets: ", json(connected).dump());
    CHECK(connected == expected);
}

TEST_CASE("native PWR_FLAGs keep their library identity and supply net") {
    const json ops = json::array({
        {{"op", "sch_add_power"}, {"net", "VCC"}, {"symbol", "PWR_FLAG"}, {"pos", {0, 0}}},
        {{"op", "sch_add_power"}, {"net", "GND"}, {"symbol", "PWR_FLAG"}, {"pos", {10.16, 0}}},
        {{"op", "sch_add_power"}, {"net", "GND"}, {"pos", {15.24, 0}}}});
    json sch = chudware::ecad::apply_ops(schema::new_schematic("Power"), ops)["doc"];
    ki::assign_uuids(sch);
    REQUIRE(sch["power"].size() == 3);
    CHECK(sch["power"][0]["ref"] == "#FLG01");
    CHECK(sch["power"][1]["ref"] == "#FLG02");
    CHECK(sch["power"][2]["ref"] == "#PWR01");
    const std::string text = ki::write_kicad_sch(sch);
    CHECK(text.find("(lib_id \"power:PWR_FLAG\")") != std::string::npos);
    json reopened = ki::import_kicad_sch(text);
    REQUIRE(reopened["power"].size() == 3);
    CHECK(reopened["power"][0]["symbol"] == "PWR_FLAG");
    CHECK(reopened["power"][0]["net"] == "VCC");
    CHECK(reopened["power"][0]["value"] == "PWR_FLAG");
    CHECK(reopened["power"][1]["symbol"] == "PWR_FLAG");
    CHECK(reopened["power"][1]["net"] == "GND");
    CHECK(reopened["power"][1]["value"] == "PWR_FLAG");
    CHECK_FALSE(reopened["power"][0]["fields"].contains("Chudware.PowerNet"));
    ki::assign_uuids(reopened);
    CHECK(ki::write_kicad_sch(reopened) == text);
}

TEST_CASE("edited modeled values retain KiCad-only children and UUIDs") {
    const std::string text = R"((kicad_sch (version 20250114) (generator "eeschema") (uuid "00000000-0000-4000-8000-000000000001")
        (paper "A4") (lib_symbols) (wire (pts (xy 1 2) (xy 3 4)) (stroke (width 0.3) (type dash))
        (uuid "00000000-0000-4000-8000-000000000002"))))";
    json sch = ki::import_kicad_sch(text);
    const auto id = sch["wires"][0]["uuid"];
    sch["wires"][0]["pts"] = {{2, -3}, {4, -5}};
    ki::assign_uuids(sch);
    const auto updated = ki::import_kicad_sch(ki::write_kicad_sch(sch));
    CHECK(updated["wires"][0]["uuid"] == id);
    CHECK(updated["wires"][0]["pts"] == sch["wires"][0]["pts"]);
    const auto& extra = updated["wires"][0]["kicad_extra"];
    bool dash = false;
    for (const auto& node : extra)
        if (node.is_array() && node.size() && node[0] == "stroke")
            for (const auto& part : node)
                if (part.is_array() && part.size() > 1 && part[0] == "type" && part[1] == "dash") dash = true;
    CHECK(dash);
}

TEST_CASE("KiCad properties preserve typed engineering and simulation data") {
    json sch = schema::new_schematic("Metadata");
    sch = chudware::ecad::apply_ops(sch, json::array({
        {{"op", "sch_add_symbol"}, {"libid", "Device:R"}, {"ref", "R1"},
         {"value", "10k"}, {"pos", {0, 0}},
         {"fields", {{"Sim.Device", "R"}, {"engineering", {{"height", 1.5}}}}},
         {"sim_params", {{"eng", {{"power_w", 0.25}}}, {"waveform", "SIN"}}}}}))["doc"];
    ki::assign_uuids(sch);
    const json reopened_sch = ki::import_kicad_sch(ki::write_kicad_sch(sch));
    REQUIRE(reopened_sch["symbols"].size() == 1);
    CHECK(reopened_sch["symbols"][0]["fields"]["Sim.Device"] == "R");
    CHECK(reopened_sch["symbols"][0]["fields"]["engineering"]["height"] == 1.5);
    CHECK(reopened_sch["symbols"][0]["sim_params"]["eng"]["power_w"] == 0.25);

    json board = schema::new_board("Metadata");
    board["footprints"].push_back({
        {"id", "fp1"}, {"fpid", "Test:NoPads"}, {"ref", "U1"}, {"value", "Controller"},
        {"pos", {5, -5}}, {"angle", 0}, {"side", "top"}, {"pads", json::array()},
        {"graphics", json::array()}, {"libid", "MCU:Controller"}, {"on_board", true},
        {"fields", {{"height", 1.6}, {"eng", {{"package", "qfn"}}}}},
        {"eng_overrides", {{"power_w", {{"typ", 2.0}}}}}});
    board["zones"].push_back({
        {"id", "z1"}, {"net", "GND"}, {"layer", "F.Cu"},
        {"polygon", {{0, 0}, {10, 0}, {10, -10}, {0, -10}}},
        {"filled_polys", {{{1, -1}, {9, -1}, {9, -9}, {1, -9}}}}});
    ki::assign_uuids(board);
    const json reopened_board = ki::import_kicad_pcb(ki::write_kicad_pcb(board));
    REQUIRE(reopened_board["footprints"].size() == 1);
    const json& fp = reopened_board["footprints"][0];
    CHECK(fp["libid"] == "MCU:Controller");
    CHECK(fp["fields"]["height"] == 1.6);
    CHECK(fp["fields"]["eng"]["package"] == "qfn");
    CHECK(fp["on_board"] == true);
    CHECK(fp["eng_overrides"]["power_w"]["typ"] == 2.0);
    REQUIRE(reopened_board["zones"].size() == 1);
    CHECK(reopened_board["zones"][0]["filled_polys"] == board["zones"][0]["filled_polys"]);
}

TEST_CASE("KiCad stackup edits preserve dielectric and copper geometry") {
    json board = schema::new_board("Stackup");
    board["stackup"] = {
        {"copper_layers", 4}, {"thickness", 1.6},
        {"layers", {
            {{"id", "F.Cu"}, {"type", "copper"}, {"thickness", 0.035}},
            {{"id", "pp1"}, {"type", "prepreg"}, {"thickness", 0.21}, {"er", 4.4}, {"material", "7628"}},
            {{"id", "In1.Cu"}, {"type", "copper"}, {"thickness", 0.015}},
            {{"id", "core1"}, {"type", "core"}, {"thickness", 1.0}, {"er", 4.6}, {"material", "FR4"}},
            {{"id", "In2.Cu"}, {"type", "copper"}, {"thickness", 0.015}},
            {{"id", "pp2"}, {"type", "prepreg"}, {"thickness", 0.21}, {"er", 4.4}, {"material", "7628"}},
            {{"id", "B.Cu"}, {"type", "copper"}, {"thickness", 0.035}}}}};
    board["nets"].push_back({{"name", "UNUSED_SIGNAL"}, {"class", "default"}});
    ki::assign_uuids(board);
    const std::string text = ki::write_kicad_pcb(board);
    const json reimported = ki::import_kicad_pcb(text);
    CHECK(reimported["stackup"]["copper_layers"] == 4);
    REQUIRE(reimported["stackup"]["layers"].size() == 7);
    CHECK(reimported["stackup"]["layers"][1]["type"] == "prepreg");
    CHECK(reimported["stackup"]["layers"][1]["thickness"] == 0.21);
    CHECK(reimported["stackup"]["layers"][1]["er"] == 4.4);
    CHECK(reimported["stackup"]["layers"][1]["material"] == "7628");
    CHECK(reimported["stackup"]["layers"][2]["id"] == "In1.Cu");
    bool found = false;
    for (const auto& net : reimported["nets"]) if (net.value("name", "") == "UNUSED_SIGNAL") found = true;
    CHECK(found);
}

TEST_CASE("copying a footprint does not reuse its pad UUID") {
    const json pad = {{"id", "p1"}, {"number", "1"}, {"pos", {0, 0}}, {"size", {1, 1}},
                      {"shape", "rect"}, {"type", "smd"}, {"layers", {"F.Cu"}},
                      {"drill", nullptr}, {"net", nullptr},
                      {"uuid", "ca75e197-6eba-42db-99e8-f961a9117b83"}};
    const json ops = json::array({
        {{"op", "pcb_add_footprint"}, {"fpid", "Test:Pad"}, {"ref", "U1"}, {"pos", {1, 1}}, {"pads", {pad}}},
        {{"op", "pcb_add_footprint"}, {"fpid", "Test:Pad"}, {"ref", "U2"}, {"pos", {3, 1}}, {"pads", {pad}}}});
    json board = chudware::ecad::apply_ops(schema::new_board("Copy"), ops)["doc"];
    CHECK_FALSE(board["footprints"][0]["pads"][0].contains("uuid"));
    CHECK_FALSE(board["footprints"][1]["pads"][0].contains("uuid"));
    ki::assign_uuids(board);
    CHECK(board["footprints"][0]["pads"][0]["uuid"] != board["footprints"][1]["pads"][0]["uuid"]);
}

TEST_CASE("multiline KiCad embedded data remains an opaque literal") {
    const std::string source = R"((kicad_pcb (version 20260206) (generator "pcbnew")
      (general (thickness 1.6)) (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
      (setup (pad_to_mask_clearance 0)) (net 0 "")
      (embedded_files (file (name "model.step") (type model)
        (data |YWJj
            ZGVm|) (checksum "1234")))))";
    json doc = ki::import_kicad_pcb(source);
    ki::assign_uuids(doc);
    const auto text = ki::write_kicad_pcb(doc);
    CHECK(text.find("|YWJj\n            ZGVm|") != std::string::npos);
    json again = ki::import_kicad_pcb(text);
    ki::assign_uuids(again);
    CHECK(ki::write_kicad_pcb(again) == text);
}

TEST_CASE("multi-segment wires get independent persistent KiCad UUIDs") {
    json sch = schema::new_schematic("Wire");
    sch["wires"].push_back({{"id", "w1"}, {"sheet", "sh1"},
                            {"pts", {{0, 0}, {10, 0}, {10, 10}}}, {"bus", false}});
    ki::assign_uuids(sch);
    const std::string text = ki::write_kicad_sch(sch);
    json again = ki::import_kicad_sch(text);
    REQUIRE(again["wires"].size() == 2);
    CHECK(again["wires"][0]["uuid"] != again["wires"][1]["uuid"]);
    ki::assign_uuids(again);
    CHECK(ki::write_kicad_sch(again) == text);
}

TEST_CASE("KiCad 10 named board nets remain editable and connected") {
    const std::string source = R"((kicad_pcb (version 20260206) (generator "pcbnew")
      (general (thickness 1.6)) (layers (0 "F.Cu" signal) (2 "B.Cu" signal))
      (setup (pad_to_mask_clearance 0))
      (footprint "Test:Pad" (layer "F.Cu") (at 1 1)
        (property "Reference" "U1") (property "Value" "Test")
        (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net "GND")))
      (segment (start 1 1) (end 2 1) (width 0.25) (layer "F.Cu") (net "GND"))
      (via (at 2 1) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net "GND"))
      (zone (net "GND") (net_name "GND") (layer "F.Cu")
        (connect_pads (clearance 0.3)) (min_thickness 0.25)
        (polygon (pts (xy 0 0) (xy 4 0) (xy 4 4) (xy 0 4))))))";
    json board = ki::import_kicad_pcb(source);
    REQUIRE(board["footprints"].size() == 1);
    REQUIRE(board["tracks"].size() == 1);
    REQUIRE(board["vias"].size() == 1);
    REQUIRE(board["zones"].size() == 1);
    CHECK(board["footprints"][0]["pads"][0]["net"] == "GND");
    CHECK(board["tracks"][0]["net"] == "GND");
    CHECK(board["vias"][0]["net"] == "GND");
    CHECK(board["zones"][0]["net"] == "GND");
    board["nets"].push_back({{"name", "VCC"}, {"class", "default"}});
    board["footprints"][0]["pads"][0]["net"] = "VCC";
    board["tracks"][0]["net"] = "VCC";
    board["vias"][0]["net"] = "VCC";
    board["zones"][0]["net"] = "VCC";
    ki::assign_uuids(board);
    const json reopened = ki::import_kicad_pcb(ki::write_kicad_pcb(board));
    CHECK(reopened["footprints"][0]["pads"][0]["net"] == "VCC");
    CHECK(reopened["tracks"][0]["net"] == "VCC");
    CHECK(reopened["vias"][0]["net"] == "VCC");
    CHECK(reopened["zones"][0]["net"] == "VCC");
}
