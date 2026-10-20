// T-RF15 (node R4a) — firmware engine surface unit tests, driving
// chudware::firmware::call directly (no routing layer). Mirrors the Python
// suites engine_oracle/firmware/test_project.py / test_pinmap.py /
// test_coverify.py / test_mcp_tools.py over the same fixture shapes.
//
// External-tool legs (arm-none-eabi-gcc / probe-rs / esptool) SKIP honestly
// when the binary is absent — the missing-tool path IS the testable behavior
// (graceful toolchain_not_found / tool_not_found degradation, contract §1/§5),
// and when the tool IS present we skip the live leg instead of faking a pass.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "environment_test_support.h"
#include "firmware/bus_models.h"
#include "firmware/emulator.h"
#include "firmware/firmware_rpc.h"
#include "firmware/pinmap.h"
#include "firmware/platform_db.h"
#include "firmware/powerprofile.h"
#include "firmware/project.h"
#include "firmware/scenario.h"
#include "firmware/sensor_models.h"
#include "firmware/sim_sensor_bridge.h"
#include "firmware/subprocess.h"
#include "firmware/private_storage.h"
#include "firmware/renode_encoding.h"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <optional>
#include <set>
#include <sstream>
#include <string>

#include <nlohmann/json.hpp>
#include <doctest/doctest.h>

using nlohmann::json;
#ifndef _WIN32
#include <unistd.h>
#include <sys/stat.h>
#endif
namespace fs = std::filesystem;

namespace {

struct EnvironmentGuard {
    std::string key;
    std::optional<std::string> previous;
    EnvironmentGuard(std::string name, const std::string& value) : key(std::move(name)) {
        if (const char* old = std::getenv(key.c_str())) previous = old;
        chudware::test::setEnvironment(key.c_str(), value.c_str());
    }
    ~EnvironmentGuard() {
        chudware::test::setOrUnsetEnvironment(key.c_str(),
            previous ? previous->c_str() : nullptr);
    }
};

#ifndef CHUDWARE_TEST_DATA
#define CHUDWARE_TEST_DATA "tests/data/firmware"
#endif

const fs::path kData = fs::path(CHUDWARE_TEST_DATA);

json read_json(const fs::path& p) {
    std::ifstream in(p);
    std::stringstream ss;
    ss << in.rdbuf();
    return json::parse(ss.str());
}

std::string read_text(const fs::path& p) {
    std::ifstream in(p, std::ios::binary);
    std::stringstream ss;
    ss << in.rdbuf();
    return ss.str();
}

// True when a LIVE emulator leg produced nothing because it ran out of wall clock, as opposed
// to running and answering wrongly. run_renode/run_qemu report a budget expiry by putting
// "timeout:" into uart_log and leaving trace_path null (core/firmware/emulator.cpp). Only that
// exact shape counts: a run that produced a trace, or failed for any other reason, still fails
// the assertions. Used by the two live legs to SKIP instead of reporting a machine-contention
// artifact as a firmware defect — see the budget note on the QEMU leg for the measurement.
bool emulator_timed_out(const json& r) {
    if (r.value("passed", false)) return false;
    if (!r.contains("trace_path") || !r["trace_path"].is_null()) return false;
    const std::string log = r.value("uart_log", std::string{});
    return log.find("timeout:") != std::string::npos;
}

// Isolated firmware root per process (mirrors the Python tests' tmp_path +
// CHUDWARE_FIRMWARE_DIR override).
struct FirmwareRootGuard {
    fs::path dir;
    FirmwareRootGuard() {
        dir = fs::temp_directory_path() /
              ("chudware-fwtest-" + std::to_string(static_cast<long long>(
                   std::chrono::system_clock::now().time_since_epoch().count())));
        chudware::firmware::private_directory(dir);
        chudware::test::setEnvironment("CHUDWARE_FIRMWARE_DIR", dir.string().c_str());
    }
    ~FirmwareRootGuard() {
        chudware::test::unsetEnvironment("CHUDWARE_FIRMWARE_DIR");
        std::error_code ec;
        fs::remove_all(dir, ec);
    }
};

json call(const std::string& method, const json& params) {
    json result;
    std::string err;
    bool handled = chudware::firmware::call(method, params, result, err);
    REQUIRE(handled);
    return result;
}

std::set<std::string> rules_of(const json& findings) {
    std::set<std::string> out;
    for (const auto& f : findings) out.insert(f.value("rule", std::string()));
    return out;
}

const json kBlinkSrc = {
    {"src/main.c",
     "#include \"pinmap.h\"\n"
     "void main(void) {\n"
     "  gpio_set_dir(PIN_PA5, GPIO_OUT);\n"
     "  while (1) { HAL_GPIO_TogglePin(GPIOA, PIN_PA5); }\n"
     "}\n"},
};

json electrical_uart_scenario() {
    json network=json::array();
    for(const char* name:{"tx","rx"}) {
        json endpoints=json::array();
        for(const char* id:{"mcu","peer"})
            endpoints.push_back({{"id",id},{"at",id},{"input_capacitance_f",5e-12},
                {"vil_v",0.8},{"vih_v",2.0},{"drive","push_pull"},
                {"v_low_v",0.0},{"v_high_v",3.3},{"r_low_ohm",20.0},{"r_high_ohm",20.0},
                {"rise_s",1e-9},{"fall_s",1e-9},{"tx_delay_s",0.0},{"rx_delay_s",0.0}});
        network.push_back({{"name",name},{"mode","single_ended"},{"model","lumped_rc"},
            {"series_r_ohm",0.0},{"wire_capacitance_f",10e-12},{"endpoints",endpoints}});
    }
    return {{"schema","aicad.fwscenario/1"},{"tracks",json::array({
        {{"kind","uart_rx"},{"bus","usart1"},{"link_id","serial"},
         {"events",json::array({{{"t_ms",2.0},{"text","A"}}})}}})},
        {"bus_links",json::array({{{"id","serial"},{"bus","usart1"},{"protocol","uart"},
            {"controller_clock_hz",16000000.0},
            {"peer",{{"baud_hz",9600.0},{"electrical_variant","ttl"}}},
            {"electrical",{{"signals",network}}}}})}};
}

}  // namespace

TEST_CASE("dispatch: unknown methods fall through") {
    json result;
    std::string err;
    CHECK_FALSE(chudware::firmware::call("bogus_method", json::object(), result, err));
    // The whole fw_* surface is ported (T-RF15 resume) — every fw_ method handles.
    for (const char* m : {"fw_sim_run", "fw_power_profile", "fw_sensor_scenario"}) {
        CHECK(chudware::firmware::call(m, json::object(), result, err));
    }
}

TEST_CASE("fw_new_project: scaffold + template + compile_commands") {
    FirmwareRootGuard guard;
    json r = call("fw_new_project", {{"name", "blinky"}, {"mcu_family", "stm32f4"}});
    REQUIRE(r.contains("project_id"));
    REQUIRE(r.contains("project"));
    const json& p = r["project"];
    CHECK(p.value("schema", "") == "aicad.firmware.project/1");
    CHECK(p.value("name", "") == "blinky");
    CHECK(p.value("mcu_family", "") == "stm32f4");
    CHECK(p.value("toolchain", "") == "cmake");
    CHECK(p.value("build_dir", "") == "build");
    CHECK(p["board_doc_ref"].is_null());
    CHECK(p["src_paths"] == json::array({"src/main.c", "src/startup.c"}));

    fs::path root = guard.dir / r["project_id"].get<std::string>();
    CHECK(fs::is_regular_file(root / "project.json"));
    CHECK(fs::is_regular_file(root / "compile_commands.json"));
    CHECK(fs::is_regular_file(root / "CMakeLists.txt"));
    CHECK(fs::is_regular_file(root / "src" / "main.c"));
    CHECK(fs::is_regular_file(root / "include" / "chudware_trace.h"));
    // The synthetic compile_commands enumerates the template sources (§6).
    json cc = read_json(root / "compile_commands.json");
    REQUIRE(cc.is_array());
    CHECK(cc.size() == 2);
    CHECK(cc[0].value("command", "").find("arm-none-eabi-gcc -c ") == 0);
}

TEST_CASE("fw_new_project: mcu auto-select from a board BOM + pinmap seed") {
    FirmwareRootGuard guard;
    json r = call("fw_new_project", {
        {"name", "auto"},
        {"board_doc_ref", (kData / "board_demo.json").string()},
    });
    REQUIRE(r.contains("project"));
    // BOM value "STM32F407" -> stm32f4 (first _FAMILY_PREFIXES hit).
    CHECK(r["project"].value("mcu_family", "") == "stm32f4");
    // The pin-map header was seeded from the board's netlist.
    REQUIRE(r.contains("pinmap_path"));
    std::string header = read_text(r["pinmap_path"].get<std::string>());
    CHECK(header == read_text(kData / "pinmap_demo.golden.h"));
}

TEST_CASE("fw_new_project: error envelopes") {
    FirmwareRootGuard guard;
    json r = call("fw_new_project", {{"mcu_family", "stm32f4"}, {"toolchain", "bogus"}});
    CHECK(r.value("code", "") == "invalid_request");
    CHECK(r["message"].get<std::string>().find("toolchain") != std::string::npos);

    // No mcu and no derivable BOM -> honest refusal, never a guessed MCU.
    r = call("fw_new_project", {{"name", "no-mcu"}});
    CHECK(r.value("code", "") == "invalid_request");
}

TEST_CASE("fw_sources: list / add / remove / unknown project") {
    FirmwareRootGuard guard;
    json created = call("fw_new_project", {{"mcu_family", "stm32f4"}});
    std::string pid = created["project_id"];

    json r = call("fw_sources", {{"project_id", pid}});
    CHECK(r["src_paths"] == json::array({"src/main.c", "src/startup.c"}));

    // Add materialises an empty stub; result is sorted.
    r = call("fw_sources", {{"project_id", pid}, {"add", json::array({"src/driver.c"})}});
    INFO(r.dump());
    REQUIRE_FALSE(r.contains("code"));
    r = call("fw_sources", {{"project_id", pid}});
    CHECK(r["src_paths"] == json::array({"src/driver.c", "src/main.c", "src/startup.c"}));
    CHECK(fs::is_regular_file(guard.dir / pid / "src" / "driver.c"));

    r = call("fw_sources", {{"project_id", pid}, {"remove", json::array({"src/startup.c"})}});
    INFO(r.dump());
    REQUIRE_FALSE(r.contains("code"));
    r = call("fw_sources", {{"project_id", pid}});
    CHECK(r["src_paths"] == json::array({"src/driver.c", "src/main.c"}));

    r = call("fw_sources", {{"project_id", "does-not-exist"}});
    CHECK(r.value("code", "") == "unknown_project");

    r = call("fw_sources", json::object());
    CHECK(r.value("code", "") == "invalid_request");
}

TEST_CASE("fw_build: graceful toolchain_not_found when the cross-compiler is absent") {
    FirmwareRootGuard guard;
    json created = call("fw_new_project", {{"mcu_family", "stm32f4"}});
    std::string pid = created["project_id"];

    bool have_cmake = !chudware::firmware::which("cmake").empty();
    bool have_gcc = !chudware::firmware::which("arm-none-eabi-gcc").empty();
    json r = call("fw_build", {{"project_id", pid}});
    if (have_cmake && have_gcc) {
        // Real build leg — the template must compile cleanly.
        INFO("cmake + arm-none-eabi-gcc present: running the real build");
        CHECK(r.value("success", false) == true);
        CHECK(r.value("returncode", -1) == 0);
        CHECK_FALSE(r["elf_path"].is_null());
    } else {
        // Honest degradation (contract §1): inside the result, never raised.
        CHECK(r.value("success", true) == false);
        CHECK(r.value("returncode", 0) == -1);
        CHECK(r["stderr"].get<std::string>().find("toolchain_not_found") == 0);
    }
    CHECK(r.contains("warnings"));
    CHECK(r.contains("cmd"));
    CHECK(r.contains("ms"));

    r = call("fw_build", {{"project_id", "does-not-exist"}});
    CHECK(r.value("code", "") == "unknown_project");
    r = call("fw_build", json::object());
    CHECK(r.value("code", "") == "invalid_request");
}

TEST_CASE("pinmap: header generation is byte-identical to the Python golden") {
    json board = read_json(kData / "board_demo.json");
    std::string header = chudware::firmware::generate_pinmap(board, "stm32f4");
    CHECK(header == read_text(kData / "pinmap_demo.golden.h"));
    // Power/ground rails never emitted.
    CHECK(header.find("PIN_VDD") == std::string::npos);
    CHECK(header.find("PIN_GND") == std::string::npos);
}

TEST_CASE("fw_verify_board: clean demo board passes") {
    json r = call("fw_verify_board", {
        {"board_doc_ref", (kData / "board_demo.json").string()},
        {"sources", kBlinkSrc},
    });
    CHECK(r.value("ok", false) == true);
    CHECK(r.value("mcu_family", "") == "stm32f4");  // auto-selected from the BOM
    CHECK(r["findings"].empty());
    CHECK(r["summary"].value("n", -1) == 0);
    CHECK(r["dynamic"].value("ran", true) == false);
    // The scan saw the PA5 write intent (gpio_set_dir ... GPIO_OUT + TogglePin).
    CHECK(r["fw_pins"].contains("PA5"));
    CHECK(r["fw_pins"]["PA5"].value("writes", 0) >= 1);
}

TEST_CASE("fw_verify_board: unknown net, strap repurpose, missing decouple") {
    json src = {
        {"src/main.c",
         "void f(void) {\n"
         "  HAL_GPIO_WritePin(GPIOA, PIN_DOES_NOT_EXIST, 1);\n"
         "  digitalWrite(PIN_BOOT0, 1);\n"
         "}\n"},
    };
    json r = call("fw_verify_board", {
        {"board_doc_ref", (kData / "board_nocap.json").string()},
        {"sources", src},
    });
    REQUIRE(r.value("ok", false) == true);
    auto rules = rules_of(r["findings"]);
    CHECK(rules.count("fw_pin_unknown_net") == 1);
    CHECK(rules.count("fw_strap_repurposed") == 1);
    CHECK(rules.count("fw_missing_decouple") == 1);
    CHECK(r["summary"].value("errors", 0) == 2);
    CHECK(r["summary"].value("warnings", 0) == 1);
    // Findings carry the ERC shape + fix hint.
    for (const auto& f : r["findings"]) {
        CHECK(f.contains("rule"));
        CHECK(f.contains("severity"));
        CHECK(f.contains("net"));
        CHECK(f.contains("pins"));
        CHECK(f["xy"].is_null());
        CHECK(f.contains("message"));
        CHECK(f.contains("fix"));
    }
}

TEST_CASE("fw_verify_board: direction clash needs the schematic pin types") {
    json src = {
        {"src/main.c", "void f(void) { digitalWrite(PIN_PA0, 1); }\n"},
    };
    // Without sch_doc_ref the direction check skips honestly (no clash finding).
    json r = call("fw_verify_board", {
        {"board_doc_ref", (kData / "board_clash.json").string()},
        {"sources", src},
    });
    CHECK(rules_of(r["findings"]).count("fw_direction_clash") == 0);

    r = call("fw_verify_board", {
        {"board_doc_ref", (kData / "board_clash.json").string()},
        {"sch_doc_ref", (kData / "sch_clash.json").string()},
        {"sources", src},
    });
    auto rules = rules_of(r["findings"]);
    CHECK(rules.count("fw_direction_clash") == 1);
}

TEST_CASE("fw_verify_board: dynamic pass over a trace artifact") {
    json r = call("fw_verify_board", {
        {"board_doc_ref", (kData / "board_demo.json").string()},
        {"sources", kBlinkSrc},
        {"trace_path", (kData / "trace_pa5_pa9.json").string()},
    });
    REQUIRE(r.value("ok", false) == true);
    CHECK(r["dynamic"].value("ran", false) == true);
    CHECK(r["dynamic"].value("source", "") == "trace");
    // PA5 toggles on a mapped net (fine); PA9 has no board net -> unmapped.
    auto rules = rules_of(r["findings"]);
    CHECK(rules.count("fw_trace_unmapped_pin") == 1);
    CHECK(rules.count("fw_trace_drive_conflict") == 0);
}

TEST_CASE("fw_verify_board: error envelopes") {
    json r = call("fw_verify_board", {
        {"board_doc_ref", "/nonexistent/board.json"},
        {"sources", kBlinkSrc},
    });
    CHECK(r.value("code", "") == "unknown_board");

    r = call("fw_verify_board", {
        {"board_doc_ref", (kData / "board_demo.json").string()},
    });
    CHECK(r.value("code", "") == "invalid_request");

    r = call("fw_verify_board", {
        {"board_doc_ref", (kData / "board_demo.json").string()},
        {"sources", kBlinkSrc},
        {"trace_path", "/nonexistent/trace.json"},
    });
    CHECK(r.value("ok", false) == true);
    CHECK(r["dynamic"].value("ran", true) == false);
    CHECK(r["dynamic"].contains("reason"));
}

TEST_CASE("fw_flash / fw_debug_*: envelopes + graceful tool_not_found") {
    json r = call("fw_flash", json::object());
    CHECK(r.value("code", "") == "invalid_request");

    r = call("fw_flash", {{"elf_path", "/tmp/fw.elf"}});
    CHECK(r.value("code", "") == "invalid_request");  // no chip for probe-rs

    r = call("fw_debug_start", json::object());
    CHECK(r.value("code", "") == "invalid_request");
    r = call("fw_debug_halt", json::object());
    CHECK(r.value("code", "") == "invalid_request");
    r = call("fw_debug_read_mem", {{"chip", "STM32F407VGTx"}});
    CHECK(r.value("code", "") == "invalid_request");  // missing address/length

    if (chudware::firmware::which("probe-rs").empty()) {
        r = call("fw_flash", {{"elf_path", "/tmp/fw.elf"}, {"chip", "STM32F407VGTx"}});
        CHECK(r.value("success", true) == false);
        CHECK(r.value("returncode", 0) == -1);
        CHECK(r["output"].get<std::string>().find("tool_not_found") == 0);

        r = call("fw_debug_start", {{"chip", "STM32F407VGTx"}});
        CHECK(r.value("success", true) == false);
        CHECK(r.value("returncode", 0) == -1);

        r = call("fw_debug_halt", {{"chip", "STM32F407VGTx"}});
        CHECK(r.value("success", true) == false);
        CHECK(r.value("returncode", 0) == -1);

        r = call("fw_debug_read_mem",
                 {{"chip", "STM32F407VGTx"}, {"address", "0x20000000"}, {"length", 16}});
        CHECK(r.value("success", true) == false);
        CHECK(r["reason"].get<std::string>().find("tool_not_found") == 0);
        CHECK(r.value("address", "") == "0x20000000");
        CHECK(r.value("length", 0) == 16);
    } else {
        // probe-rs present: a live flash/debug needs a real probe attached —
        // skip honestly rather than fake a pass.
        MESSAGE("probe-rs present; live flash/debug leg skipped (needs a DAP probe)");
    }

    // esptool route: explicit port + .bin.
    if (chudware::firmware::which("esptool.py").empty()) {
        r = call("fw_flash", {{"elf_path", "/tmp/fw.bin"}, {"port", "/dev/ttyUSB0"}});
        CHECK(r.value("success", true) == false);
        CHECK(r.value("returncode", 0) == -1);
        CHECK(r["cmd"][0] == "esptool.py");
    } else {
        MESSAGE("esptool.py present; live ESP32 flash leg skipped (needs a board)");
    }
}

TEST_CASE("fw_status: doctor payload shape (tools may or may not be installed)") {
    json r = call("fw_status", json::object());
    CHECK(r.value("ok", false) == true);
    REQUIRE(r.contains("tools"));
    for (const char* t : {"cmake", "ninja", "arm-none-eabi-gcc", "renode", "qemu-system-arm",
                          "probe-rs", "esptool", "idf.py", "arduino-cli"}) {
        REQUIRE(r["tools"].contains(t));
        CHECK(r["tools"][t].contains("found"));
        CHECK(r["tools"][t].contains("install_hint"));
        CHECK(r["tools"][t].contains("unlocks"));
    }
    REQUIRE(r.contains("families"));
    for (const char* f : {"cortex-m", "esp32", "avr"}) {
        REQUIRE(r["families"].contains(f));
        CHECK(r["families"][f].contains("can_build"));
        CHECK(r["families"][f].contains("can_emulate"));
        CHECK(r["families"][f].contains("can_flash"));
        CHECK(r["families"][f].contains("missing"));
    }
    // T-FIN2 decisions baked into the matrix: esp32 can never emulate; avr
    // cannot flash (no wired path).
    CHECK(r["families"]["esp32"].value("can_emulate", true) == false);
    CHECK(r["families"]["avr"].value("can_flash", true) == false);
    REQUIRE(r.contains("platforms"));
    CHECK(r["platforms"].contains("stm32f4"));
    CHECK(r["platforms"]["stm32f4"].value("gpio_observe", false) == true);
    CHECK(r["platforms"]["esp32"].value("emulation", "") == "none");
    CHECK(r.contains("banners"));
    CHECK(r.contains("missing"));
}

TEST_CASE("fw_projects: lists sidecars newest-first, skips corrupt ones") {
    FirmwareRootGuard guard;
    json r = call("fw_projects", json::object());
    REQUIRE(r.contains("projects"));
    CHECK(r["projects"].empty());

    json a = call("fw_new_project", {{"name", "first"}, {"mcu_family", "stm32f4"}});
    json b = call("fw_new_project", {{"name", "second"}, {"mcu_family", "stm32f4"}});
    // A half-written project must not take down the whole list.
    fs::create_directories(guard.dir / "corrupt000");
    std::ofstream(guard.dir / "corrupt000" / "project.json") << "{not json";

    r = call("fw_projects", json::object());
    REQUIRE(r["projects"].size() == 2);
    CHECK(r["projects"][0].value("root_dir", "").find(guard.dir.string()) == 0);
    // newest first
    double c0 = r["projects"][0].value("created_at", 0.0);
    double c1 = r["projects"][1].value("created_at", 0.0);
    CHECK(c0 >= c1);
    (void)a;
    (void)b;
}

// ── T-RF15 resume: the emulator leg (fw_sim_run / fw_power_profile /
// fw_sensor_scenario) ──────────────────────────────────────────────────────

TEST_CASE("emulator parsers: gpio/uart events + ODR snapshot (pure)") {
    std::string raw =
        "noise line\n"
        "CHUDWARE_GPIO_EVT PA5=1\n"
        "CHUDWARE_GPIO_EVT PA5=0 t=10ms\n"
        "CHUDWARE_GPIO_EVT PB3=1 t=2500us\n"
        "CHUDWARE_GPIO_EVT PB3=0 t=1s\n"
        "CHUDWARE_T_MARK 100\n"
        "hello world\n"
        "CHUDWARE_GPIO_ODR_BEGIN A\n"
        "0x00000021\n"
        "CHUDWARE_GPIO_ODR_END A\n";
    json ev = chudware::firmware::parse_gpio_events(raw);
    REQUIRE(ev.size() == 4);
    CHECK(ev[0].value("pin", "") == "PA5");
    CHECK(ev[0]["t_ms"].is_null());  // never substitute host time for a missing target time
    CHECK(ev[1].value("t_ms", 0.0) == 10.0);
    CHECK(ev[2].value("t_ms", 0.0) == 2.5);  // us
    CHECK(ev[3].value("t_ms", 0.0) == 1000.0);  // s
    CHECK(ev[0].value("seq", -1) == 1);

    json uart = chudware::firmware::parse_uart_events(raw);
    // noise (pre-mark), hello world (after mark@100), and the raw ODR hex line
    // (not a marker — Python's filter only excludes *_EVT / *_ODR_ lines).
    REQUIRE(uart.size() == 3);
    CHECK(uart[0]["t_ms"].is_null());
    CHECK(uart[1].value("t_ms", 0.0) == 100.0);
    CHECK(uart[1].value("text", "") == "hello world");
    CHECK(uart[2].value("text", "") == "0x00000021");

    json odr = chudware::firmware::parse_renode_gpio_odr(raw);
    CHECK(odr.value("PA0", -1) == 1);
    CHECK(odr.value("PA5", -1) == 1);
    CHECK(odr.value("PA1", -1) == 0);
    CHECK(odr.size() == 16);

    // ODR snapshot wins over the last event on merge.
    json states = chudware::firmware::derive_gpio_states(ev, odr);
    CHECK(states.value("PA5", -1) == 1);
    CHECK(states.value("PB3", -1) == 0);
}

TEST_CASE("UART consumers retain complete flight messages despite unrelated bus caps") {
    using namespace chudware::firmware;
    std::ostringstream raw;
    const auto i2c_traffic = [&] {
        for (int i = 0; i < 5000; ++i)
            raw << "CHUDWARE_BUS_EVT t_ns=" << i
                << " bus=i2c1 protocol=i2c event=register_read value=0\n";
    };
    const auto transmit = [&](const std::string& bus, unsigned char byte, int t_ms) {
        raw << "CHUDWARE_BUS_EVT t_ns=" << t_ms * 1000000
            << " bus=" << bus << " protocol=uart event=tx_complete value="
            << static_cast<unsigned int>(byte) << "\n";
    };
    i2c_traffic();
    // Neither sample loss nor another protocol's emitter loss drops UART TX data.
    raw << "CHUDWARE_BUS_EVT t_ns=1 bus=i2c1 protocol=i2c event=trace_truncated cause=event_limit\n"
        << "CHUDWARE_BUS_EVT t_ns=2 bus=usart1 protocol=uart event=trace_truncated cause=sample_limit\n"
        << "monitor metadata: METADATA_ONLY\n";
    const std::string message = "BARO_CLIMB\nMAG_TURN";
    for (size_t i = 0; i < message.size(); ++i) {
        transmit("usart1", static_cast<unsigned char>(message[i]), 100 + static_cast<int>(i));
        transmit("usart2", 'x', 100 + static_cast<int>(i));
    }
    std::string binary = "\r\n";
    binary.push_back('\0');
    binary += "\xC3\xA9";
    for (unsigned char byte : binary) transmit("usart1", byte, 130);
    for (unsigned char byte : std::string("CROSS_")) transmit("usart3", byte, 130);
    for (unsigned char byte : std::string("BUS")) transmit("usart4", byte, 130);
    // Malformed transmit values and records without timestamps stay ignored.
    raw << "CHUDWARE_BUS_EVT t_ns=131000000 bus=usart1 protocol=uart event=tx_complete value=invalid\n"
        << "CHUDWARE_BUS_EVT bus=usart1 protocol=uart event=tx_complete value=65\n";
    i2c_traffic();

    const auto buses = parse_bus_events(raw.str());
    REQUIRE(buses.size() == 4096);
    CHECK(buses[2047].value("type", "") == "trace_truncated");
    const auto uart = parse_uart_events(raw.str());
    std::map<std::string, std::string> streams;
    for (size_t i = 0; i < uart.size(); ++i) {
        CHECK(uart[i].value("type", "") == "uart");
        CHECK(uart[i].value("seq", -1LL) == static_cast<long long>(i));
        streams[uart[i].value("bus", "")] += uart[i].value("text", "");
    }
    REQUIRE(streams["usart1"] == message + binary);
    CHECK(streams["usart2"] == std::string(message.size(), 'x'));
    CHECK(streams["usart3"] == "CROSS_");
    CHECK(streams["usart4"] == "BUS");
    CHECK(uart[2 * message.size() - 2].value("t_ms", -1.0) == 118.0);
    const auto verdicts = evaluate_assertions(raw.str(), json::object(),
        {"uart:BARO_CLIMB", "MAG_TURN", "uart_within:MAG_TURN@118ms",
         "uart_within:MAG_TURN@117ms", "uart:METADATA_ONLY", "uart:CROSS_BUS",
         "uart_within:CROSS_BUS@130ms"}, json::array(), uart);
    REQUIRE(verdicts.size() == 7);
    CHECK(verdicts[0].value("passed", false));
    CHECK(verdicts[1].value("passed", false));
    CHECK(verdicts[2].value("passed", false));
    CHECK_FALSE(verdicts[3].value("passed", true));
    CHECK(verdicts[3].value("detail", "").find("first match at t=118ms") != std::string::npos);
    CHECK_FALSE(verdicts[4].value("passed", true));
    CHECK_FALSE(verdicts[5].value("passed", true));
    CHECK_FALSE(verdicts[6].value("passed", true));
}

TEST_CASE("UART consumers refuse genuine UART cap and emitter loss") {
    using namespace chudware::firmware;
    SUBCASE("UART's own 4096 event cap") {
        const std::string message = "BARO_CLIMB";
        std::ostringstream raw;
        for (size_t i = 0; i < 5000; ++i)
            raw << "CHUDWARE_BUS_EVT t_ns=" << i * 1000000
                << " bus=usart1 protocol=uart event=tx_complete value="
                << static_cast<unsigned int>(i < message.size() ? message[i] : 'x') << "\n";
        const auto uart = parse_uart_events(raw.str());
        REQUIRE(uart.size() == 4096);
        CHECK(uart[2047].value("type", "") == "trace_truncated");
        CHECK(uart[2047].value("dropped", 0) == 905);
        const auto verdicts = evaluate_assertions(raw.str(), json::object(),
            {"uart_within:BARO_CLIMB@20ms", "uart:BARO_CLIMB"}, json::array(), uart);
        REQUIRE(verdicts.size() == 2);
        for (const auto& verdict : verdicts) {
            CHECK_FALSE(verdict.value("passed", true));
            CHECK(verdict.value("detail", "").find("not evaluated") == 0);
        }
    }
    SUBCASE("UART or unscoped emitter loss") {
        for (const std::string scope : {"bus=usart1 protocol=uart ", ""}) {
            std::string raw =
                "CHUDWARE_BUS_EVT t_ns=1000000 bus=usart1 protocol=uart event=tx_complete value=79\n"
                "CHUDWARE_BUS_EVT t_ns=2000000 " + scope +
                "event=trace_truncated cause=event_limit\n"
                "CHUDWARE_BUS_EVT t_ns=3000000 bus=usart1 protocol=uart event=tx_complete value=75\n";
            const auto uart = parse_uart_events(raw);
            REQUIRE(uart.size() == 3);
            CHECK(uart[1].value("type", "") == "trace_truncated");
            const auto verdicts = evaluate_assertions(raw, json::object(),
                {"uart_within:OK@10ms", "uart:OK"}, json::array(), uart);
            REQUIRE(verdicts.size() == 2);
            for (const auto& verdict : verdicts) {
                CHECK_FALSE(verdict.value("passed", true));
                CHECK(verdict.value("detail", "").find("not evaluated") == 0);
            }
        }
    }
}

TEST_CASE("emulator traces are bounded, ordered, and disclose truncation") {
    std::ostringstream raw;
    for (int i = 0; i < 5000; ++i)
        raw << "CHUDWARE_GPIO_EVT PA5=" << (i % 2) << " t=" << i << "ms\n";
    json events = chudware::firmware::parse_gpio_events(raw.str());
    REQUIRE(events.size() == 4096);
    REQUIRE(events[2047].value("type", "") == "trace_truncated");
    CHECK(events[2047].value("dropped", 0) == 905);
    for (size_t i = 1; i < events.size(); ++i)
        CHECK(events[i - 1].value("seq", -1LL) <= events[i].value("seq", -1LL));

    json states = chudware::firmware::derive_gpio_states(events, json::object());
    CHECK(states.value("PA5", -1) == 1);  // the retained tail preserves final state
    json assertions = chudware::firmware::evaluate_assertions(
        raw.str(), states, {"toggles:PA5<5000"}, events, json::array());
    REQUIRE(assertions.size() == 1);
    CHECK(assertions[0].value("passed", true) == false);
    CHECK(assertions[0].value("detail", "").find("event limit") != std::string::npos);

    fs::path elf = fs::temp_directory_path() / "chudware_bounded_trace.elf";
    std::ofstream(elf, std::ios::binary | std::ios::trunc) << "trace fixture";
    auto trace_path = chudware::firmware::write_trace_artifact(
        elf.string(), "test", raw.str(), events, states);
    REQUIRE(trace_path.has_value());
    json trace = read_json(*trace_path);
    CHECK(trace.value("truncated", false) == true);
    CHECK(trace["events"].size() == 4096);
    for (size_t i = 1; i < trace["events"].size(); ++i)
        CHECK(trace["events"][i - 1].value("seq", -1LL) <=
              trace["events"][i].value("seq", -1LL));
    std::error_code ec;
    fs::remove(*trace_path, ec);
    fs::remove(elf, ec);
}

TEST_CASE("emulator rejects invalid setup before probing a backend") {
    fs::path bad = fs::temp_directory_path() / "chudware_invalid_firmware.elf";
    std::ofstream(bad, std::ios::binary | std::ios::trunc) << "not an elf";

    auto result = chudware::firmware::run_simulation(bad.string(), "cortex-m4", {}, 30.0);
    CHECK(result.passed == false);
    CHECK(result.uart_log.find("invalid_image:") == 0);

    result = chudware::firmware::run_simulation(
        bad.string(), "cortex-m4", {"gpio:PZ99=1"}, 30.0);
    CHECK(result.passed == false);
    CHECK(result.uart_log.find("invalid_setup:") == 0);
    CHECK(result.uart_log.find("invalid pin") != std::string::npos);

    result = chudware::firmware::run_simulation(
        bad.string(), "cortex-m4", {"uart:ready"},
        std::numeric_limits<double>::infinity());
    CHECK(result.passed == false);
    CHECK(result.uart_log.find("invalid_setup: timeout_s") == 0);

    std::error_code ec;
    fs::remove(bad, ec);
}


TEST_CASE("Renode refuses unavailable timestamped GPIO observation") {
#ifndef _WIN32
    fs::path dir = fs::temp_directory_path() /
                   ("chudware-renode-capability-" + std::to_string(static_cast<long long>(
                        std::chrono::steady_clock::now().time_since_epoch().count())));
    fs::create_directories(dir);
    fs::path invoked = dir / "invoked";
    fs::path renode = dir / "renode";
    {
        std::ofstream script(renode, std::ios::binary | std::ios::trunc);
        script << "#!/bin/sh\n"
                  "echo invoked > "
               << invoked.string() << "\n"
                  "exit 0\n";
    }
    fs::permissions(renode, fs::perms::owner_all, fs::perm_options::add);
    fs::path elf = dir / "valid.elf";
    {
        std::ofstream image(elf, std::ios::binary | std::ios::trunc);
        char header[52] = {};
        header[0] = '\x7f';
        header[1] = 'E';
        header[2] = 'L';
        header[3] = 'F';
        header[4] = 1;
        header[5] = 1;
        header[6] = 1;
        header[16] = 2;
        header[18] = 40;
        image.write(header, sizeof(header));
    }

    const char* old_raw = std::getenv("PATH");
    const std::optional<std::string> old_path =
        old_raw ? std::optional<std::string>(old_raw) : std::nullopt;
    chudware::test::setEnvironment("PATH", dir.string().c_str());
    auto timed = chudware::firmware::run_simulation(
        elf.string(), "stm32f4",
        {"gpio:PA5=1@>10ms", "toggles:PA5>=2", "period:PA5=10ms+-1ms"}, 2.0);
    if (old_path)
        chudware::test::setEnvironment("PATH", old_path->c_str());
    else
        chudware::test::unsetEnvironment("PATH");

    CHECK(timed.passed == false);
    CHECK(timed.uart_log.find("platform_capability:") == 0);
    REQUIRE(timed.assertion_results.size() == 3);
    for (const auto& assertion : timed.assertion_results)
        CHECK_FALSE(assertion.value("passed", true));

    CHECK_FALSE(fs::exists(invoked));

    std::error_code ec;
    fs::remove_all(dir, ec);
#else
    CHECK(true);
#endif
}

TEST_CASE("backend process faults cannot become successful simulation") {
#ifndef _WIN32
    fs::path dir = fs::temp_directory_path() / "chudware-fault-emulator";
    fs::create_directories(dir);
    fs::path qemu = dir / "qemu-system-arm";
    {
        std::ofstream script(qemu, std::ios::binary | std::ios::trunc);
        script << "#!/bin/sh\n"
                  "echo synthetic-emulator-fault >&2\n"
                  "exit 7\n";
    }
    fs::permissions(qemu, fs::perms::owner_all, fs::perm_options::add);
    fs::path elf = dir / "valid.elf";
    {
        std::ofstream image(elf, std::ios::binary | std::ios::trunc);
        char header[52] = {};
        header[0] = '\x7f';
        header[1] = 'E';
        header[2] = 'L';
        header[3] = 'F';
        header[4] = 1;   // ELFCLASS32
        header[5] = 1;   // little-endian
        header[6] = 1;   // current ELF version
        header[16] = 2;  // ET_EXEC
        header[18] = 40; // EM_ARM
        image.write(header, sizeof(header));
    }

    const char* old_raw = std::getenv("PATH");
    const std::optional<std::string> old_path =
        old_raw ? std::optional<std::string>(old_raw) : std::nullopt;
    chudware::test::setEnvironment("PATH", dir.string().c_str());
    auto result = chudware::firmware::run_simulation(
        elf.string(), "cortex-m4", {}, 2.0);
    if (old_path)
        chudware::test::setEnvironment("PATH", old_path->c_str());
    else
        chudware::test::unsetEnvironment("PATH");

    CHECK(result.passed == false);
    CHECK(result.uart_log.find("emulator_fault: qemu-system-arm exited with code 7") == 0);
    CHECK(result.uart_log.find("synthetic-emulator-fault") != std::string::npos);
    std::error_code ec;
    fs::remove_all(dir, ec);
#else
    CHECK(true);
#endif
}

TEST_CASE("evaluate_assertions: full grammar (pure)") {
    using chudware::firmware::evaluate_assertions;
    json gpio_events = json::array({
        {{"type", "gpio"}, {"seq", 1}, {"t_ms", 10.0}, {"pin", "PA5"}, {"level", 1}},
        {{"type", "gpio"}, {"seq", 2}, {"t_ms", 60.0}, {"pin", "PA5"}, {"level", 0}},
        {{"type", "gpio"}, {"seq", 3}, {"t_ms", 110.0}, {"pin", "PA5"}, {"level", 1}},
    });
    json uart_events = json::array({
        {{"type", "uart"}, {"seq", 4}, {"t_ms", 50.0}, {"text", "sensor ready"}},
    });
    json states = {{"PA5", 1}, {"PB0", 0}};

    auto by_name = [](const json& rs, const std::string& name) -> json {
        for (const auto& r : rs)
            if (r.value("name", "") == name) return r;
        return json();
    };

    std::vector<std::string> assertions = {
        "uart:hello", "plain-text", "missing-text",
        "gpio:PA5=1", "gpio:PB0=0", "gpio:PA5=0", "gpio:PA9=1",
        "gpio:PA5=1@>5ms", "gpio:PA5=0@<100ms", "gpio:PA5=1@110ms", "gpio:PA5=1@>200ms",
        "toggles:PA5>=2", "toggles:PA5==3", "toggles:PA5==2",
        "period:PA5=100ms±5ms", "period:PA5=50ms+-5ms",
        "uart_within:sensor ready@60ms", "uart_within:sensor ready@10ms",
    };
    json rs = evaluate_assertions("say hello and plain-text", states, assertions,
                                  gpio_events, uart_events);
    REQUIRE(rs.size() == assertions.size());
    CHECK(by_name(rs, "uart:hello").value("passed", false) == true);
    CHECK(by_name(rs, "plain-text").value("passed", false) == true);
    CHECK(by_name(rs, "missing-text").value("passed", true) == false);
    CHECK(by_name(rs, "gpio:PA5=1").value("passed", false) == true);
    CHECK(by_name(rs, "gpio:PB0=0").value("passed", false) == true);
    CHECK(by_name(rs, "gpio:PA5=0").value("passed", true) == false);
    CHECK(by_name(rs, "gpio:PA9=1")["detail"].get<std::string>().find("not in gpio_states") !=
          std::string::npos);
    CHECK(by_name(rs, "gpio:PA5=1@>5ms").value("passed", false) == true);
    CHECK(by_name(rs, "gpio:PA5=0@<100ms").value("passed", false) == true);
    CHECK(by_name(rs, "gpio:PA5=1@110ms").value("passed", false) == true);
    CHECK(by_name(rs, "gpio:PA5=1@>200ms").value("passed", true) == false);
    CHECK(by_name(rs, "toggles:PA5>=2").value("passed", false) == true);
    CHECK(by_name(rs, "toggles:PA5==2").value("passed", false) == true);
    CHECK(by_name(rs, "toggles:PA5==3").value("passed", true) == false);
    // rising edges at t=10 and t=110 → mean period 100ms
    CHECK(by_name(rs, "period:PA5=100ms±5ms").value("passed", false) == true);
    CHECK(by_name(rs, "period:PA5=50ms+-5ms").value("passed", true) == false);
    CHECK(by_name(rs, "uart_within:sensor ready@60ms").value("passed", false) == true);
    CHECK(by_name(rs, "uart_within:sensor ready@10ms").value("passed", true) == false);
}

TEST_CASE("scenario: validation + fixtures + plan (pure)") {
    using namespace chudware::firmware;

    // Fixture library
    auto names = list_fixtures();
    CHECK(names.size() == 5);
    json fx = get_fixture("user_button");
    REQUIRE(fx.is_object());
    CHECK(fx.value("schema", "") == "aicad.fwscenario/1");
    CHECK(validate_scenario(fx).empty());
    // thermistor fixture carries 32 ADC counts clamped to 12 bits
    json adc = get_fixture("adc_thermistor");
    CHECK(adc["tracks"][0]["waveform"]["values"].size() == 32);

    // load_scenario forms
    auto [s1, e1] = load_scenario("fixture:user_button");
    CHECK(s1.is_object());
    CHECK(e1.empty());
    auto [s2, e2] = load_scenario("no-such-fixture");
    CHECK(s2.is_null());
    REQUIRE(e2.size() == 1);
    auto [s3, e3] = load_scenario("{not json");
    CHECK(s3.is_null());
    CHECK(e3.size() == 1);
    auto [s4, e4] = load_scenario(json{{"schema", "wrong"}, {"tracks", json::array()}});
    CHECK(e4.size() >= 2);  // bad schema + empty tracks

    // Invalid input remains rejected independently of diagnostic wording.
    json bad_pin = {{"schema", "aicad.fwscenario/1"},
                    {"tracks", json::array({{{"kind", "gpio_edges"},
                                             {"pin", "P99"},
                                             {"events", json::array({{{"t_ms", 1}, {"level", 1}}})}}})}};
    auto errs = validate_scenario(bad_pin);
    REQUIRE(errs.size() == 1);

    // Plan: user_button on the stm32f4 platform → Button attach + 6 timeline actions
    json caps = get_capabilities("stm32f4");
    std::string platform_path = get_platform_file("stm32f4");
    REQUIRE(!platform_path.empty());
    std::ifstream in(platform_path);
    std::stringstream ss;
    ss << in.rdbuf();
    ScenarioPlan plan = build_scenario_plan(fx, caps, ss.str());
    REQUIRE(plan.errors.empty());
    CHECK(plan.duration_ms == doctest::Approx(1080.0));

    // ADC track on nrf52 → honest refusal (no stub window registered)
    ScenarioPlan adc_plan = build_scenario_plan(adc, get_capabilities("nrf52"), "");
    REQUIRE(adc_plan.errors.size() == 1);

    // gpio stimulus on a platform without the port → honest refusal
    ScenarioPlan no_port = build_scenario_plan(fx, get_capabilities("rp2040"), "");
    CHECK(!no_port.errors.empty());

}

TEST_CASE("bus links: selected track propagation excludes branched same-net copper") {
    using namespace chudware::firmware;
    json track={{"id","route"},{"net","SERIAL"},{"layer","F.Cu"},{"width",0.2},
                {"p1",{0.0,0.0}},{"p2",{10.0,0.0}}};
    json board={{"stackup",{{"layers",json::array({
        {{"id","F.Cu"},{"type","copper"},{"thickness",0.035}},
        {{"id","core"},{"type","dielectric"},{"thickness",0.2},{"er",4.2}},
        {{"id","B.Cu"},{"type","copper"},{"thickness",0.035}}})}}},
        {"tracks",json::array({track})}};
    auto resolve=[&](const json& source) {
        auto scenario=electrical_uart_scenario();
        auto& signal=scenario["bus_links"][0]["electrical"]["signals"][1];
        signal["model"]="lossless_tline";
        signal["wire_capacitance_f"]=0.0;
        signal["segments"]=json::array({{{"from","mcu"},{"to","peer"},{"ecad_source",source}}});
        std::vector<std::string> errors;
        auto resolved=resolve_bus_links(scenario,get_capabilities("stm32f4"),
                                       read_text(get_platform_file("stm32f4")),errors);
        REQUIRE(errors.empty());
        REQUIRE_FALSE(resolved.empty());
        return resolved[0]["electrical"]["signals"][1]["segments"][0];
    };
    const json baseline=resolve({{"board",board},{"track_or_net","route"}});
    auto branch=track;
    branch["id"]="branch";
    branch["p2"]={0.0,1000.0};
    board["tracks"].push_back(branch);
    for(const json& selector:std::vector<json>{json("route"),track}) {
        const json source={{"board",board},{"track_or_net",selector}};
        const json segment=resolve(source);
        CHECK(segment["propagation_s"]==baseline["propagation_s"]);
        CHECK(segment["ecad_reference"]["length_mm"]==10.0);
        CHECK(segment["ecad_reference"]["net"]=="SERIAL");
        CHECK(segment["ecad_source"]==source);
    }
    auto scenario=electrical_uart_scenario();
    auto& signal=scenario["bus_links"][0]["electrical"]["signals"][1];
    signal["model"]="lossless_tline";
    signal["wire_capacitance_f"]=0.0;
    signal["segments"]=json::array({{{"from","mcu"},{"to","peer"},
        {"ecad_source",{{"board",board},{"track_or_net","SERIAL"}}}}});
    CHECK_FALSE(validate_scenario(scenario).empty());
    const json explicit_path=resolve({{"board",board},{"track_or_net","SERIAL"},{"length_mm",25.0}});
    CHECK(explicit_path["propagation_s"].get<double>()==
          doctest::Approx(baseline["propagation_s"].get<double>()*2.5));
    signal["segments"][0]["propagation_s"]=1e-9;
    CHECK(validate_scenario(scenario).empty());
}

TEST_CASE("fw_sim_run: malformed nested bus links return invalid_scenario") {
    FirmwareRootGuard guard;
    const auto project=call("fw_new_project",{{"mcu_family","stm32f4"}});
    const std::string pid=project["project_id"];
    fs::create_directories(guard.dir/pid/"build");
    std::ofstream(guard.dir/pid/"build"/"app.elf")<<"fake";
    const auto valid=electrical_uart_scenario();
    REQUIRE(chudware::firmware::validate_scenario(valid).empty());
    const std::vector<std::pair<std::string,json>> malformed={
        {"/bus_links/0/peer",nullptr},{"/bus_links/0/peer","bad"},
        {"/bus_links/0/peer",json::array()},{"/bus_links/0/peer/baud_hz",json::object()},
        {"/bus_links/0/electrical",json::array()},
        {"/bus_links/0/electrical/signals",json::object()},
        {"/bus_links/0/electrical/signals/0",nullptr},
        {"/bus_links/0/electrical/signals/0/endpoints",json::object()},
        {"/bus_links/0/electrical/signals/0/endpoints/0",json::array()},
        {"/bus_links/0/electrical/signals/0/endpoints/0/vih_v","bad"},
        {"/bus_links/0/electrical/signals/0/segments",nullptr},
        {"/bus_links/0/electrical/signals/0/segments",json::object()},
        {"/bus_links/0/electrical/signals/0/bias",false},
        {"/bus_links/0/electrical/signals/0/terminations",json::array({nullptr})},
        {"/bus_links/0/sources",json::object()},{"/bus_links/0/limitations","bad"}};
    for(const auto& [path,value]:malformed) {
        INFO("malformed field: ",path);
        auto scenario=valid;
        scenario[json::json_pointer(path)]=value;
        json result;
        REQUIRE_NOTHROW(result=call("fw_sim_run",{{"project_id",pid},{"scenario",scenario}}));
        CHECK(result.value("code","")=="invalid_scenario");
    }
    auto missing_bus=valid;
    missing_bus["bus_links"][0].erase("bus");
    json result;
    REQUIRE_NOTHROW(result=call("fw_sim_run",{{"project_id",pid},{"scenario",missing_bus}}));
    CHECK(result.value("code","")=="invalid_scenario");
    auto invalid_ecad=valid;
    auto& signal=invalid_ecad["bus_links"][0]["electrical"]["signals"][0];
    signal["model"]="lossless_tline";
    signal["wire_capacitance_f"]=0.0;
    signal["segments"]=json::array({{{"from","mcu"},{"to","peer"},
        {"z0_ohm",50.0},{"propagation_s",1e-9},{"ecad_source",false}}});
    REQUIRE_NOTHROW(result=call("fw_sim_run",{{"project_id",pid},{"scenario",invalid_ecad}}));
    CHECK(result.value("code","")=="invalid_scenario");
}

TEST_CASE("fw_sim_run: envelopes + no-elf + esp32 refusal + invalid scenario") {
    FirmwareRootGuard guard;
    json r = call("fw_sim_run", json::object());
    CHECK(r.value("code", "") == "invalid_request");
    r = call("fw_sim_run", {{"project_id", "nope"}});
    CHECK(r.value("code", "") == "unknown_project");

    // No ELF in the build dir → graceful SimResult, never an envelope.
    json proj = call("fw_new_project", {{"mcu_family", "stm32f4"}});
    std::string pid = proj["project_id"];
    r = call("fw_sim_run", {{"project_id", pid}});
    CHECK(r.value("passed", true) == false);
    CHECK(r["uart_log"].get<std::string>().find("no_elf:") == 0);

    // Invalid scenario → invalid_scenario envelope. Note the Python handler
    // resolves the ELF FIRST (no_elf graceful result wins), so plant one.
    fs::create_directories(guard.dir / pid / "build");
    std::ofstream(guard.dir / pid / "build" / "app.elf") << "fake";
    r = call("fw_sim_run", {{"project_id", pid},
                            {"scenario", json{{"schema", "wrong"}}}});
    CHECK(r.value("code", "") == "invalid_scenario");

    // ESP32 target: emulation == "none" → platform_capability refusal (T-FIN2).
    json esp = call("fw_new_project", {{"mcu_family", "esp32"}});
    std::string epid = esp["project_id"];
    fs::path ebuild = guard.dir / epid / "build";
    fs::create_directories(ebuild);
    std::ofstream(ebuild / "app.elf") << "not a real elf";
    r = call("fw_sim_run", {{"project_id", epid}, {"elf_path", (ebuild / "app.elf").string()},
                            {"assertions", json::array({"uart:hi"})}});
    CHECK(r.value("passed", true) == false);
    CHECK(r["uart_log"].get<std::string>().find("platform_capability:") == 0);
    CHECK(r["assertion_results"][0]["detail"].get<std::string>().find("build + flash only") !=
          std::string::npos);
}

// Compile the semihosting hello fixture when the cross-compiler is available;
// returns "" when it cannot be built (test then SKIPs honestly).
static std::string build_semi_hello(const fs::path& dir) {
    if (chudware::firmware::which("arm-none-eabi-gcc").empty()) return "";
    fs::path elf = dir / "semi_hello.elf";
    std::vector<std::string> cmd = {
        "arm-none-eabi-gcc", "-mcpu=cortex-m3", "-mthumb", "-nostdlib", "-nostartfiles",
        "-Os", "-T", (kData / "semi_hello.ld").string(),
        (kData / "semi_hello.c").string(), "-o", elf.string(),
    };
    auto rr = chudware::firmware::run_capture(cmd, "", 60.0);
    if (rr.returncode != 0) return "";
    std::error_code ec;
    return fs::is_regular_file(elf, ec) ? elf.string() : "";
}

TEST_CASE("fw_sim_run LIVE: QEMU refuses firmware requiring native semihosting") {
    bool have_qemu = !chudware::firmware::which("qemu-system-arm").empty();
    FirmwareRootGuard guard;
    std::string elf = build_semi_hello(guard.dir);
    if (!have_qemu || elf.empty()) {
        MESSAGE("SKIP live QEMU leg: qemu-system-arm or arm-none-eabi-gcc absent");
        CHECK(true);
        return;
    }
    json proj = call("fw_new_project", {{"mcu_family", "cortex-m4"}});
    std::string pid = proj["project_id"];
    // The fixture asks for semihosting console/exit. That host-service interface
    // is no longer enabled: it must not produce verified console evidence.
    const json r = call("fw_sim_run", {{"project_id", pid},
                                     {"elf_path", elf},
                                     {"backend", "qemu"},
                                     {"assertions", json::array({"uart:hello world"})},
                                     {"timeout_s", 2.0}});
    CHECK_FALSE(r.value("passed", true));
    CHECK(r.value("uart_log", std::string()).find("hello world") == std::string::npos);
    const std::string uart = r.value("uart_log", std::string());
    const bool refused_by_fault = uart.rfind("emulator_fault:", 0) == 0;
    const bool refused_by_timeout = uart.rfind("timeout:", 0) == 0;
    CHECK((refused_by_fault || refused_by_timeout));
}

TEST_CASE("fw_sim_run LIVE: Renode UART path on stm32f4 (honest skip without tools)") {
    bool have_renode = !chudware::firmware::which("renode").empty();
    FirmwareRootGuard guard;
    std::string elf;
    if (!chudware::firmware::which("arm-none-eabi-gcc").empty()) {
        elf = (guard.dir / "stm32_hello.elf").string();
        auto rr = chudware::firmware::run_capture(
            {"arm-none-eabi-gcc", "-mcpu=cortex-m4", "-mthumb", "-nostdlib", "-nostartfiles",
             "-Os", "-T", (kData / "stm32_hello.ld").string(),
             (kData / "stm32_hello.c").string(), "-o", elf},
            "", 60.0);
        if (rr.returncode != 0) elf.clear();
    }
    if (!have_renode || elf.empty()) {
        MESSAGE("SKIP live Renode leg: renode or arm-none-eabi-gcc absent");
        CHECK(true);
        return;
    }
    json proj = call("fw_new_project", {{"mcu_family", "stm32f4"}});
    std::string pid = proj["project_id"];
    // UART-only assertion → no AddStateChangedHook lines in the .resc (the
    // hook command errors on Renode 1.16.1 and aborts the script — same in
    // the Python oracle). The live run captures "hello renode" from USART1.
    // A cold Renode startup can exceed the live fixture budget under contention.
    json r = call("fw_sim_run", {{"project_id", pid},
                                 {"elf_path", elf},
                                 {"assertions", json::array({"uart:hello renode"})},
                                 {"timeout_s", 180.0}});
    INFO("uart_log: ", r["uart_log"].get<std::string>().substr(0, 600));
    if (emulator_timed_out(r)) {
        MESSAGE("SKIP live Renode leg: emulator did not finish in budget (machine contention) - "
                << r.value("uart_log", std::string{}));
        return;
    }
    CHECK(r.value("passed", false) == true);
    CHECK(r["uart_log"].get<std::string>().find("hello renode") != std::string::npos);
    REQUIRE_FALSE(r["trace_path"].is_null());
    json trace = read_json(r["trace_path"].get<std::string>());
    CHECK(trace.value("source", "") == "renode");
}

TEST_CASE("fw_sim_run LIVE: UART text delivers exact UTF-8 bytes through firmware DR") {
    using namespace chudware::firmware;
    if(which("renode").empty() || which("arm-none-eabi-gcc").empty()) {
        MESSAGE("SKIP live UART byte leg: renode or arm-none-eabi-gcc absent");
        return;
    }
    FirmwareRootGuard guard;
    const fs::path source=guard.dir/"uart_bytes.c", elf=guard.dir/"uart_bytes.elf";
    {
        std::ofstream out(source);
        out<<R"C(#include <stdint.h>
#define REG(a) (*(volatile uint32_t *)(a))
#define SR REG(0x40011000u)
#define DR REG(0x40011004u)
extern uint32_t _estack;
void Reset_Handler(void);
__attribute__((section(".isr_vector")))
void (*const vectors[])(void)={(void (*)(void))&_estack,Reset_Handler};
void Reset_Handler(void) {
    static const uint8_t expected[]={65,66,67,13,10,0,34,92,195,169,240,159,152,128};
    REG(0x40023844u)|=1u<<4;
    REG(0x40011008u)=139u;
    REG(0x4001100cu)=(1u<<13)|(1u<<3)|(1u<<2);
    // At 16 MHz this boot byte takes about 87 us on the wire.
    DR='!';
    while(!(SR&(1u<<6))) {}
    REG(0x40011008u)=1667u;
    unsigned good=1;
    for(unsigned i=0;i<sizeof(expected);++i) {
        while(!(SR&(1u<<5))) {}
        if((uint8_t)DR!=expected[i]) good=0;
    }
    const char *message=good?"UART_BYTES_OK":"UART_BYTES_BAD";
    while(*message) {
        while(!(SR&(1u<<7))) {}
        DR=(uint8_t)*message++;
    }
    for(;;) {}
}
)C";
    }
    const auto build=run_capture({"arm-none-eabi-gcc","-mcpu=cortex-m4","-mthumb",
        "-nostdlib","-nostartfiles","-Os","-T",(kData/"stm32_hello.ld").string(),
        source.string(),"-o",elf.string()},"",60.0);
    INFO(build.stderr_text);
    REQUIRE(build.returncode==0);
    std::string text="ABC\r\n";
    text.push_back('\0');
    text+="\"\\";
    text+="\xC3\xA9\xF0\x9F\x98\x80";
    json scenario={{"schema","aicad.fwscenario/1"},
        {"tracks",json::array({{{"kind","uart_rx"},{"uart","usart1"},
            {"baud_hz",16000000.0/1667.0},
            {"events",json::array({{{"t_ms",2.0},{"text",text}}})}}})}};
    const auto project=call("fw_new_project",{{"mcu_family","stm32f4"}});
    const auto short_result=call("fw_sim_run",{{"project_id",project["project_id"]},
        {"elf_path",elf.string()},{"run_s",0.00005},
        {"assertions",json::array({"uart:!"})},{"timeout_s",180.0}});
    if(emulator_timed_out(short_result)) {
        MESSAGE("SKIP live 50 us UART horizon leg: emulator exceeded wall-clock budget");
        return;
    }
    INFO(short_result.value("uart_log",std::string{}));
    REQUIRE(short_result.contains("bus_events"));
    bool boot_byte_started=false;
    for(const auto& event:short_result["bus_events"]) {
        REQUIRE(event.contains("t_ns"));
        REQUIRE(event["t_ns"].is_number_unsigned());
        CHECK(event["t_ns"].get<unsigned long long>()<=50000);
        if(event.value("bus","")!="usart1") continue;
        CHECK(event.value("event","")!="tx_complete");
        CHECK(event.value("event","")!="rx_complete");
        CHECK_FALSE((event.value("event","")=="register_read" &&
                     event.value("register","")=="DR"));
        if(event.value("event","")=="tx_start" && event.value("value","")=="33") {
            boot_byte_started=true;
            CHECK(std::stoull(event.at("end_ns").get<std::string>())>50000);
        }
    }
    REQUIRE(boot_byte_started);
    REQUIRE_FALSE(short_result.value("passed",true));
    REQUIRE(short_result["assertion_results"].size()==1);
    CHECK_FALSE(short_result["assertion_results"][0].value("passed",true));
    CHECK(short_result["assertion_results"][0].value("detail","").find("not found")!=
          std::string::npos);
    const auto result=call("fw_sim_run",{{"project_id",project["project_id"]},
        {"elf_path",elf.string()},{"scenario",scenario},{"run_s",0.04},
        {"assertions",json::array({"uart:UART_BYTES_OK"})},{"timeout_s",180.0}});
    if(emulator_timed_out(result)) {
        MESSAGE("SKIP live UART byte leg: emulator exceeded wall-clock budget");
        return;
    }
    INFO(result.value("uart_log",std::string{}));
    REQUIRE(result.value("passed",false));
    std::vector<int> expected,consumed,completed;
    for(unsigned char byte:text) expected.push_back(byte);
    REQUIRE(result.contains("bus_events"));
    bool boot_byte_completed=false;
    for(const auto& event:result["bus_events"]) {
        if(event.value("bus","")!="usart1") continue;
        if(event.value("event","")=="tx_complete" && event.value("value","")=="33") {
            boot_byte_completed=true;
            CHECK(event["t_ns"].get<unsigned long long>()>50000);
            CHECK(event["t_ns"].get<unsigned long long>()<=100000);
        }
        if(event.value("event","")=="register_read" && event.value("register","")=="DR")
            consumed.push_back(std::stoi(event.at("value").get<std::string>()));
        if(event.value("event","")=="rx_complete") {
            CHECK(event.value("outcome","")=="sampled");
            completed.push_back(std::stoi(event.at("value").get<std::string>()));
        }
    }
    CHECK(consumed==expected);
    CHECK(completed==expected);
    CHECK(boot_byte_completed);
}

TEST_CASE("fw_power_profile: activity-only + board rails + envelopes") {
    FirmwareRootGuard guard;
    // Canned trace: CPU sleep 100..300ms of a 300ms window; PA5 high 200ms of 300.
    fs::path trace_file = guard.dir / "trace.json";
    {
        std::ofstream out(trace_file);
        out << read_text(kData / "trace_power.json");
    }

    // Activity-only (no board): cpu_markers residency + stamped pin duty.
    json r = call("fw_power_profile", {{"trace_path", trace_file.string()}});
    CHECK(r.value("schema", "") == "aicad.powerprofile/1");
    CHECK(r["cpu"].value("source", "") == "cpu_markers");
    CHECK(r["cpu"].value("active_frac", 0.0) == doctest::Approx(1.0 / 3.0).epsilon(1e-5));
    CHECK(r["cpu"].value("sleep_frac", 1.0) == doctest::Approx(2.0 / 3.0).epsilon(1e-5));
    CHECK(r["pins"]["PA5"].value("source", "") == "stamped_events");
    CHECK(r["pins"]["PA5"].value("duty", 0.0) == doctest::Approx(2.0 / 3.0).epsilon(1e-5));
    CHECK(r["pins"]["PA5"].value("period_s", 0.0) == doctest::Approx(0.2));
    CHECK(r["rails"].empty());
    CHECK(r.value("window_ms", 0.0) == doctest::Approx(300.0));

    // With the eng board: VDD = duty(1/3)*20mA + (2/3)*10uA; VDDA floor note.
    r = call("fw_power_profile", {{"trace_path", trace_file.string()},
                                  {"board_doc_ref", (kData / "board_eng.json").string()}});
    REQUIRE(r["rails"].size() == 2);
    CHECK(r.value("mcu_family", "") == "");  // fw_power_profile has no mcu_family echo key
    json rails = r["rails"];
    const json* vdd = nullptr;
    const json* vdda = nullptr;
    for (const auto& rail : rails) {
        if (rail.value("name", "") == "VDD") vdd = &rail;
        if (rail.value("name", "") == "VDDA") vdda = &rail;
    }
    REQUIRE(vdd != nullptr);
    REQUIRE(vdda != nullptr);
    CHECK(vdd->value("i_peak_a", 0.0) == doctest::Approx(0.02));
    CHECK(vdd->value("i_avg_a", 0.0) ==
          doctest::Approx(0.02 / 3.0 + (2.0 / 3.0) * 1e-5).epsilon(1e-4));
    CHECK((*vdd)["residency"].value("active_frac", 0.0) ==
          doctest::Approx(1.0 / 3.0).epsilon(1e-5));
    CHECK((*vdd)["schedule"].value("period_s", 0.0) == doctest::Approx(0.2));
    CHECK(vdda->value("i_avg_a", 0.0) == doctest::Approx(0.001 / 3.0).epsilon(1e-4));
    CHECK(r["coverage"].value("components_total", 0) == 3);
    CHECK(r["coverage"].value("components_with_rails", 0) == 1);

    // Envelopes
    r = call("fw_power_profile", json::object());
    CHECK(r.value("code", "") == "invalid_request");
    r = call("fw_power_profile", {{"trace_path", "/nonexistent.json"}});
    CHECK(r.value("code", "") == "invalid_trace");
    r = call("fw_power_profile",
             {{"trace_path", trace_file.string()}, {"project_id", "nope"}});
    CHECK(r.value("code", "") == "unknown_project");
}

TEST_CASE("fw_sensor_scenario: bridge builds a valid scenario + envelopes") {
    json profile = {
        {"t_s", {0.0, 0.05, 0.1}},
        {"altitude_m", {100.0, 105.0, 110.0}},
        {"yaw_rad", {0.0, 0.1, 0.2}},
        {"airspeed_m_s", 25.0},
    };
    json sensor_map = json::array({
        {{"device", "mpu6050"}},
        {{"device", "bmp280"}},
        {{"device", "neo6m"}, {"uart", "usart1"}},
        {{"device", "pitot"}},
    });
    json r = call("fw_sensor_scenario", {{"profile", profile}, {"sensor_map", sensor_map},
                                         {"dt_s", 0.05}});
    REQUIRE(r.contains("scenario"));
    CHECK(r.value("n_tracks", 0) == 4);
    CHECK(r["devices"] == json::array({"mpu6050", "bmp280", "neo6m", "pitot"}));
    CHECK(r["scenario"].value("schema", "") == "aicad.fwscenario/1");
    // IMU stream: sample-and-hold grid, 14-byte burst responses.
    const json& imu = r["scenario"]["tracks"][0];
    CHECK(imu.value("kind", "") == "i2c_register_stream");
    CHECK(imu.value("address", 0) == 0x68);
    CHECK(imu["samples"].size() == 3);
    CHECK(imu["samples"][0]["response"].size() == 14);
    REQUIRE(imu["samples"][0]["registers"].is_object());
    CHECK(imu["samples"][0]["registers"].value("117", -1) == 0x68);  // WHO_AM_I
    for (int offset = 0; offset < 14; ++offset)
        CHECK(imu["samples"][0]["registers"].value(std::to_string(0x3B + offset), -1) ==
              imu["samples"][0]["response"][offset].get<int>());
    for (const auto& sample : imu["samples"]) {
        CHECK(sample.contains("t_ms"));
        CHECK(sample["registers"].is_object());
    }
    const json& baro = r["scenario"]["tracks"][1];
    CHECK(baro["samples"][0]["registers"].value("208", -1) == 0x58);  // CHIP_ID
    CHECK(baro["samples"][0]["registers"] != baro["samples"][2]["registers"]);
    // GNSS went uart_rx with NMEA text.
    const json& gnss = r["scenario"]["tracks"][2];
    CHECK(gnss.value("kind", "") == "uart_rx");
    CHECK(gnss["events"][0]["text"].get<std::string>().find("$GPGGA,") == 0);
    // Pitot: 2-byte big-endian counts.
    CHECK(r["scenario"]["tracks"][3]["samples"][0]["response"].size() == 2);

    r = call("fw_sensor_scenario", json::object());
    CHECK(r.value("code", "") == "invalid_request");
    r = call("fw_sensor_scenario", {{"profile", profile}, {"sensor_map", json::array()}});
    CHECK(r.value("code", "") == "invalid_request");
    r = call("fw_sensor_scenario",
             {{"profile", profile},
              {"sensor_map", json::array({{{"device", "fluxcapacitor"}}})}});
    CHECK(r.value("code", "") == "invalid_request");
}

TEST_CASE("sensor models preserve units, altitude reference, and deterministic stepping") {
    using namespace chudware::firmware;
    json profile_json = {
        {"t_s", {0.0, 1.0}},
        {"pos_ned_m", json::array({json::array({0.0, 0.0, 0.0}),
                                    json::array({10.0, 0.0, 0.0})})},
        {"vel_ned_m_s", json::array({json::array({50.0, 0.0, 0.0}),
                                      json::array({50.0, 0.0, 0.0})})},
        {"airspeed_m_s", {50.0, 50.0}},
        {"temperature_c", 15.0},
        {"origin_alt_m", 1000.0},
    };
    FlightProfile profile = FlightProfile::from_dict(profile_json);
    json baro = bmp280_series(profile.samples, profile.origin_alt_m);
    REQUIRE(baro.size() == 2);
    CHECK(baro[0].value("t_ms", -1.0) == doctest::Approx(0.0));
    CHECK(baro[1].value("t_ms", -1.0) == doctest::Approx(1000.0));
    CHECK(baro[0].value("altitude_m", 0.0) == doctest::Approx(1000.0));
    CHECK(baro[0].value("pressure_pa", 0.0) == doctest::Approx(isa_pressure_pa(1000.0)));

    json pitot = pitot_series(profile.samples, profile.airspeed_m_s, profile.origin_alt_m);
    const double expected_density =
        isa_pressure_pa(1000.0) / (kIsaR * (15.0 + 273.15));
    CHECK(pitot[0].value("density_kg_m3", 0.0) == doctest::Approx(expected_density));
    CHECK(pitot[0].value("q_pa", 0.0) ==
          doctest::Approx(0.5 * expected_density * 50.0 * 50.0));

    json sensor_map = json::array({
        {{"device", "bmp280"}},
        {{"device", "pitot"}},
    });
    json first = build_sensor_scenario(profile_json, sensor_map, json(), 0.25, "fixed");
    json second = build_sensor_scenario(profile_json, sensor_map, json(), 0.25, "fixed");
    CHECK(first == second);
    CHECK(first["meta"].value("n_samples", 0) == 5);
    CHECK(first["tracks"][0]["samples"][4].value("t_ms", 0.0) ==
          doctest::Approx(1000.0));

    json sample_rows = json::array({
        {{"t_s", 0.0}, {"temperature_c", 20.0}},
        {{"t_s", 1.0}, {"temperature_c", 21.0}},
    });
    json from_rows = build_sensor_scenario(
        sample_rows, json::array({{{"device", "bmp280"}}}), json(), 1.0, "rows");
    CHECK(from_rows["meta"].value("n_samples", 0) == 2);
}

TEST_CASE("sensor scenario setup rejects meaningless or unbounded inputs") {
    using namespace chudware::firmware;
    const json valid = {
        {"t_s", {0.0, 1.0}},
        {"altitude_m", {100.0, 101.0}},
        {"airspeed_m_s", {10.0, 10.0}},
    };
    const json one_sensor = json::array({{{"device", "bmp280"}}});

    CHECK_THROWS_AS(
        FlightProfile::from_dict({{"t_s", {0.0, 0.0}}}), std::invalid_argument);
    CHECK_THROWS_AS(
        FlightProfile::from_dict({
            {"t_s", {0.0, 1.0}},
            {"altitude_m", {100.0}},
        }), std::runtime_error);
    CHECK_THROWS_AS(
        FlightProfile::from_dict({
            {"t_s", {0.0}},
            {"quat_body_from_ned", json::array({json::array({0.0, 0.0, 0.0, 0.0})})},
        }), std::invalid_argument);
    CHECK_THROWS_AS(
        FlightProfile::from_dict({
            {"t_s", {0.0}},
            {"airspeed_m_s", {-1.0}},
        }), std::invalid_argument);

    CHECK_THROWS_AS(
        build_sensor_scenario(valid, one_sensor, json(), 0.0, "bad-dt"),
        std::invalid_argument);
    CHECK_THROWS_AS(
        build_sensor_scenario(valid, one_sensor, -1.0, 0.1, "bad-duration"),
        std::invalid_argument);
    CHECK_THROWS_AS(
        build_sensor_scenario(
            valid, json::array({{{"device", "bmp280"}, {"address", 0x80}}}),
            json(), 0.1, "bad-address"),
        std::invalid_argument);
    CHECK_THROWS_AS(
        build_sensor_scenario(
            valid, json::array({
                {{"device", "bmp280"}, {"bus", "i2c1"}, {"address", 0x40}},
                {{"device", "pitot"}, {"bus", "i2c1"}, {"address", 0x40}},
            }), json(), 0.1, "conflict"),
        std::invalid_argument);
    CHECK_THROWS_AS(
        build_sensor_scenario(
            valid, json::array({{{"device", "bmp280"}, {"kind", "uart_rx"}}}),
            json(), 0.1, "bad-kind"),
        std::invalid_argument);
    CHECK_THROWS_AS(
        build_sensor_scenario(valid, one_sensor, 100.0, 0.001, "too-many"),
        std::invalid_argument);
}

TEST_CASE("fw_verify_board refuses dynamic evidence requiring native semihosting") {
    FirmwareRootGuard guard;
    bool have_qemu = !chudware::firmware::which("qemu-system-arm").empty();
    std::string elf = build_semi_hello(guard.dir);
    if (!have_qemu || elf.empty()) {
        MESSAGE("SKIP live emulate=true leg: qemu-system-arm or arm-none-eabi-gcc absent");
        CHECK(true);
        return;
    }
    json proj = call("fw_new_project", {{"mcu_family", "cortex-m4"}});
    std::string pid = proj["project_id"];
    // Plant the built ELF in the project's build dir.
    fs::path bdir = guard.dir / pid / "build";
    fs::create_directories(bdir);
    fs::copy_file(elf, bdir / "semi_hello.elf");
    json r = call("fw_verify_board", {
        {"board_doc_ref", (kData / "board_demo.json").string()},
        {"sources", json{{"src/main.c", "void f(void) { digitalWrite(PIN_PA5, 1); }\n"}}},
        {"project_id", pid},
        {"emulate", true},
        {"timeout_s", 2.0},
    });
    REQUIRE(r.value("ok", false) == true);
    INFO("dynamic: ", r["dynamic"].dump());
    // Native semihosting markers are not available as physical GPIO evidence.
    CHECK_FALSE(r["dynamic"].value("ran", true));
    CHECK(r["dynamic"]["source"].is_null());
    CHECK_FALSE(r["dynamic"].value("reason", "").empty());
    // The no-ELF degrade is still intact:
    json proj2 = call("fw_new_project", {{"mcu_family", "stm32f4"}});
    r = call("fw_verify_board", {
        {"board_doc_ref", (kData / "board_demo.json").string()},
        {"sources", json{{"src/main.c", "void f(void) { digitalWrite(PIN_PA5, 1); }\n"}}},
        {"project_id", proj2["project_id"]},
        {"emulate", true},
    });
    CHECK(r["dynamic"].value("reason", "") == "no_elf: run fw_build first");
}

TEST_CASE("firmware scenario admits work and monitor identifiers before expansion") {
    using namespace chudware::firmware;
    json quadrature{{"schema", kScenarioSchema}, {"tracks", json::array({
        {{"kind", "quadrature"}, {"pin_a", "PA0"}, {"pin_b", "PA1"},
         {"steps", 100000000}, {"step_ms", 1.0}}})}};
    CHECK_FALSE(validate_scenario(quadrature).empty());
    auto caps = get_capabilities("stm32f4");
    auto plan = build_scenario_plan(quadrature, caps, "gpioPortA:");
    CHECK_FALSE(plan.errors.empty());
    CHECK(plan.timeline.empty());
    quadrature["tracks"][0]["steps"] = 8;
    plan = build_scenario_plan(quadrature, caps, "gpioPortA:", 4.0);
    CHECK_FALSE(plan.errors.empty());
    CHECK(plan.timeline.empty());
    plan = build_scenario_plan(quadrature, caps, "gpioPortA:", 8.0);
    REQUIRE(plan.errors.empty());
    CHECK(plan.timeline.size() == 8);
    quadrature["tracks"] = json::array({quadrature["tracks"][0], quadrature["tracks"][0]});
    for (auto& track : quadrature["tracks"]) track["steps"] = 20000;
    CHECK_FALSE(validate_scenario(quadrature).empty());

    json can{{"schema", kScenarioSchema}, {"tracks", json::array({
        {{"kind", "can_frames"}, {"endpoint", "peer"},
         {"events", json::array({{{"t_ms", 0}, {"id", 1}, {"payload_hex", "01"}}})},
         {"drive_events", json::array({{{"t_ms", 1}, {"endpoint", "peer"}, {"state", 0}}})}}})}};
    for (const std::string hostile : {"peer\" 0\necho \"AUDIT_MARK\"\n#", "peer\\escape", "peer;quit"}) {
        can["tracks"][0]["drive_events"][0]["endpoint"] = hostile;
        CHECK_FALSE(validate_scenario(can).empty());
        CHECK(build_scenario_plan(can, caps, "can1:").timeline.empty());
    }
    can["tracks"][0]["drive_events"][0]["endpoint"] = "undeclared";
    CHECK_FALSE(build_scenario_plan(can, caps, "can1:").errors.empty());
#ifdef _WIN32
    CHECK(renode_path("C:/private/input-safe.elf") == "@C:/private/input-safe.elf");
#else
    CHECK(renode_path("/private/input-safe.elf") == "@/private/input-safe.elf");
#endif
    CHECK_THROWS_AS(renode_path("/private/input with spaces.elf"), std::invalid_argument);
    CHECK_THROWS_AS(renode_path("/private/quoted\".elf"), std::invalid_argument);
    CHECK_THROWS_AS(renode_path("/private/input;quit.elf"), std::invalid_argument);
    CHECK_THROWS_AS(renode_path("relative.elf"), std::invalid_argument);
    CHECK_THROWS_AS(renode_string("peer\nquit"), std::invalid_argument);
}

TEST_CASE("firmware fake backends receive copied inputs and no native host services") {
#ifndef _WIN32
    using namespace chudware::firmware;
    FirmwareRootGuard root;
    const auto dir = private_directory(root.dir / "fake");
    EnvironmentGuard path("PATH", dir.string());
    EnvironmentGuard credential("CHUDWARE_FW_CREDENTIAL", "synthetic-secret");
    const auto elf = dir / "input with spaces.elf";
    {
        char header[52] = {};
        header[0] = '\x7f'; header[1] = 'E'; header[2] = 'L'; header[3] = 'F';
        header[4] = 1; header[5] = 1; header[6] = 1; header[16] = 2; header[18] = 40;
        std::ofstream image(elf, std::ios::binary);
        image.write(header, sizeof(header));
    }
    for (const std::string backend : {"qemu-system-arm", "renode"}) {
        const auto log = dir / (backend + ".record");
        const auto executable = dir / backend;
        std::ofstream script(executable);
        script << "#!/bin/sh\n"
               << "{ printf 'credential=%s\\n' \"$CHUDWARE_FW_CREDENTIAL\"; "
               << "printf 'arg=%s\\n' \"$@\"; printf 'cwd=%s\\n' \"$PWD\";\n";
        if (backend == "renode") script << "/bin/cat \"$3\"\n";
        else script << "test -f \"$6\" || exit 8\n";
        script << "} > '" << log.string() << "'\nexit 0\n";
        script.close();
        fs::permissions(executable, fs::perms::owner_all);
        json scenario;
        if (backend == "renode")
            scenario = {{"schema", kScenarioSchema}, {"tracks", json::array({
                {{"kind", "can_frames"}, {"endpoint", "peer"},
                 {"events", json::array({{{"t_ms", 0}, {"id", 1}, {"payload_hex", "AB"}}})}}})}};
        const auto result = run_simulation(elf.string(), "stm32f4", {}, 2.0, scenario, 0,
                                           backend == "renode" ? "renode" : "qemu");
        REQUIRE(result.passed);
        const auto recorded = read_text(log);
        CHECK(recorded.find("credential=\n") != std::string::npos);
        CHECK(recorded.find("synthetic-secret") == std::string::npos);
        CHECK(recorded.find(elf.string()) == std::string::npos);
        CHECK(recorded.find("semihosting") == std::string::npos);
        CHECK(recorded.find("target=native") == std::string::npos);
        if (backend == "renode") {
            CHECK(recorded.find("sysbus.can1 SubmitFrame \"peer\" 1 false false \"AB\"") != std::string::npos);
            CHECK(recorded.find("LoadELF @") != std::string::npos);
            CHECK(recorded.find("LoadPlatformDescription @") != std::string::npos);
            CHECK(recorded.find("include @") != std::string::npos);
            CHECK(recorded.find("@\"") == std::string::npos);
        }
    }
#endif
}

TEST_CASE("firmware capture refuses output overflow while draining both streams") {
#ifndef _WIN32
    using namespace chudware::firmware;
    FirmwareRootGuard root;
    const auto dir = private_directory(root.dir / "capture");
    const auto executable = dir / "finite-output";
    std::ofstream script(executable);
    script << "#!/bin/sh\ni=0\nwhile [ \"$i\" -lt 4096 ]; do\n"
           << "printf '0123456789abcdef0123456789abcdef\\n'\n"
           << "printf 'fedcba9876543210fedcba9876543210\\n' >&2\n"
           << "i=$((i + 1))\ndone\n";
    script.close();
    fs::permissions(executable, fs::perms::owner_all);
    const auto result = run_capture({executable.string()}, dir.string(), 2, true, 65536);
    CHECK(result.output_limit_exceeded);
    CHECK(result.returncode == -1);
    CHECK_FALSE(result.timed_out);
    CHECK(result.stdout_text.size() <= 32768);
    CHECK(result.stderr_text.size() <= 32768);
    CHECK(result.stdout_text.size() + result.stderr_text.size() <= 65536);
    const auto closed = run_capture({"/bin/sh", "-c", "exec 1>&- 2>&-; /bin/sleep 1"},
                                    dir.string(), 0.05, true);
    CHECK(closed.timed_out);
#endif
}

TEST_CASE("firmware private storage refuses symlinks without overwriting a sentinel") {
#ifndef _WIN32
    using namespace chudware::firmware;
    FirmwareRootGuard root;
    const auto sentinel = root.dir / "sentinel";
    std::ofstream(sentinel) << "preserve";
    const auto link = root.dir / "foreign";
    fs::create_directory_symlink(root.dir, link);
    CHECK_THROWS(private_directory(link / "cache"));
    CHECK(read_text(sentinel) == "preserve");
    const auto dir = private_directory(root.dir / "owned");
    fs::create_symlink(sentinel, dir / "platform.repl");
    CHECK_THROWS(private_storage_detail::atomic_write(dir / "platform.repl", "replace"));
    CHECK(read_text(sentinel) == "preserve");
    struct stat info {};
    REQUIRE(::stat(dir.c_str(), &info) == 0);
    CHECK(info.st_uid == ::geteuid());
    CHECK((info.st_mode & 077) == 0);
#endif
}

TEST_CASE("firmware streaming capture preserves argv and finite output on every platform") {
    using namespace chudware::firmware;
    auto python = which("python3");
    if (python.empty()) python = which("python");
    if (python.empty()) { MESSAGE("SKIP cross-platform capture: Python absent"); return; }
    FirmwareRootGuard root;
    SUBCASE("finite stdout and stderr are drained while the child runs") {
        const auto result = run_capture({python, "-c",
            "import os; os.write(1,b'o'*65536); os.write(2,b'e'*65536)"},
            root.dir.string(), 5, true);
        CHECK(result.returncode == 0);
        CHECK_FALSE(result.output_limit_exceeded);
        CHECK(result.stdout_text == std::string(65536, 'o'));
        CHECK(result.stderr_text == std::string(65536, 'e'));
    }
    SUBCASE("quoted and trailing-backslash arguments retain their identity") {
        const std::vector<std::string> values{"", "two words", "a\"b", "C:\\space dir\\", "slashes\\\\\"quote"};
        std::vector<std::string> argv{python, "-c", "import json,sys; print(json.dumps(sys.argv[1:]))"};
        argv.insert(argv.end(), values.begin(), values.end());
        const auto result = run_capture(argv, root.dir.string(), 5, true);
        REQUIRE(result.returncode == 0);
        CHECK(json::parse(result.stdout_text) == json(values));
    }
    SUBCASE("finite stderr overflow is explicitly incomplete") {
        const auto result = run_capture({python, "-c", "import os; os.write(2,b'x'*131072)"},
                                        root.dir.string(), 5, true, 65536);
        CHECK(result.returncode == -1);
        CHECK(result.output_limit_exceeded);
        CHECK(result.stderr_text.size() <= 32768);
        CHECK(result.stdout_text.size() + result.stderr_text.size() <= 65536);
    }
    SUBCASE("output overflow kills descendants before they write a marker") {
        const auto marker = root.dir / "child-survived";
        const auto result = run_capture({python, "-c",
            "import os,subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
            "'import pathlib,sys,time; time.sleep(1.5); pathlib.Path(sys.argv[1]).write_text(\"escaped\")',"
            "sys.argv[1]]); os.write(1,b'x'*131072); time.sleep(10)", marker.string()},
            root.dir.string(), 5, true, 65536);
        CHECK(result.output_limit_exceeded);
        const auto pause = run_capture({python, "-c", "import time; time.sleep(2)"},
                                       root.dir.string(), 5, true);
        REQUIRE(pause.returncode == 0);
        CHECK_FALSE(fs::exists(marker));
    }
}

TEST_CASE("firmware cache and project atomic writes refuse symlink takeover") {
#ifndef _WIN32
    using namespace chudware::firmware;
    FirmwareRootGuard root;
    const auto base = firmware_root();
    const auto sentinel = base / "sentinel";
    private_storage_detail::atomic_write(sentinel, "preserve");
    const auto project = new_project(std::nullopt, "stm32f4", "cmake", "private");
    fs::remove(project.sidecar());
    fs::create_symlink(sentinel, project.sidecar());
    CHECK_THROWS(save(project));
    CHECK(read_text(sentinel) == "preserve");
    fs::remove(project.sidecar());
    fs::create_symlink(sentinel, project.root_dir() / ".project.json.tmp");
    CHECK_NOTHROW(save(project));
    CHECK(read_text(sentinel) == "preserve");
    const auto platforms = private_directory(base / "platforms");
    fs::create_symlink(sentinel, platforms / "stm32f4_discovery.repl");
    CHECK_THROWS(get_platform_file("stm32f4"));
    CHECK(read_text(sentinel) == "preserve");
    CHECK_THROWS(load("../outside"));
    const auto outside = base / "outside.c";
    const auto refused = call("fw_sources", {{"project_id", project.project_id}, {"add", json::array({"../outside.c"})}});
    CHECK(refused.value("code", "") == "invalid_request");
    CHECK_FALSE(fs::exists(outside));
#endif
}

TEST_CASE("firmware storage rejects unsafe ownership permissions and oversized reads") {
#ifndef _WIN32
    using namespace chudware::firmware;
    FirmwareRootGuard guard;
    const auto root = firmware_root();
    const auto file = root / "private.txt";
    private_storage_detail::atomic_write(file, "small");
    struct stat info {};
    REQUIRE(::lstat(file.c_str(), &info) == 0);
    CHECK((info.st_mode & 0777) == 0600);
    CHECK_THROWS(private_storage_detail::read_file(file, 4));
    CHECK(private_storage_detail::read_file(file, 5) == "small");
    fs::permissions(file, fs::perms::group_write, fs::perm_options::add);
    CHECK_THROWS(private_storage_detail::read_file(file));
    CHECK_THROWS(private_storage_detail::atomic_write(file, "replace"));
    CHECK(read_text(file) == "small");
    const auto writable = root / "writable";
    fs::create_directory(writable);
    fs::permissions(writable, fs::perms::others_write, fs::perm_options::add);
    CHECK_THROWS(private_directory(writable / "child"));
    CHECK_FALSE(fs::exists(writable / "child"));
    if (::geteuid() == 0) {
        const auto foreign = root / "foreign";
        fs::create_directory(foreign);
        REQUIRE(::chown(foreign.c_str(), 12345, 12345) == 0);
        CHECK_THROWS(private_directory(foreign));
    }
#endif
}

TEST_CASE("firmware temporary reservations and persistent per-user defaults are private") {
    using namespace chudware::firmware;
    FirmwareRootGuard guard;
    const auto root = firmware_root();
    const auto first = private_temp_directory("emulator");
    const auto second = private_temp_directory("emulator");
    CHECK(first != second);
    CHECK(first.parent_path() == root / "tmp");
    CHECK(second.parent_path() == root / "tmp");
    CHECK_THROWS(private_temp_directory("../escape"));
    EnvironmentGuard no_override("CHUDWARE_FIRMWARE_DIR", "");
#ifdef _WIN32
    EnvironmentGuard appdata("APPDATA", (guard.dir / "roaming").string());
    CHECK(firmware_root() == guard.dir / "roaming" / "Chudware" / "firmware");
#else
    EnvironmentGuard home("HOME", guard.dir.string());
    {
        EnvironmentGuard xdg("XDG_DATA_HOME", (guard.dir / "xdg").string());
        CHECK(firmware_root() == guard.dir / "xdg" / "Chudware" / "firmware");
    }
    EnvironmentGuard no_xdg("XDG_DATA_HOME", "");
    CHECK(firmware_root() == guard.dir / ".local" / "share" / "Chudware" / "firmware");
#endif
    CHECK(fs::path(get_platform_file("stm32f4")).parent_path() == firmware_root() / "platforms");
}
