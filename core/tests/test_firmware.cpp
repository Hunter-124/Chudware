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
#include "firmware/emulator.h"
#include "firmware/firmware_rpc.h"
#include "firmware/pinmap.h"
#include "firmware/platform_db.h"
#include "firmware/powerprofile.h"
#include "firmware/project.h"
#include "firmware/scenario.h"
#include "firmware/subprocess.h"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <set>
#include <sstream>
#include <string>

#include <nlohmann/json.hpp>
#include <doctest/doctest.h>

using nlohmann::json;
namespace fs = std::filesystem;

namespace {

// setenv/unsetenv portability (MSVC has _putenv only).
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

// Isolated firmware root per process (mirrors the Python tests' tmp_path +
// CHUDWARE_FIRMWARE_DIR override).
struct FirmwareRootGuard {
    fs::path dir;
    FirmwareRootGuard() {
        dir = fs::temp_directory_path() /
              ("chudware-fwtest-" + std::to_string(static_cast<long long>(
                   std::chrono::system_clock::now().time_since_epoch().count())));
        fs::create_directories(dir);
        set_env("CHUDWARE_FIRMWARE_DIR", dir.string());
    }
    ~FirmwareRootGuard() {
        unset_env("CHUDWARE_FIRMWARE_DIR");
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
    CHECK(r["src_paths"] == json::array({"src/driver.c", "src/main.c", "src/startup.c"}));
    CHECK(fs::is_regular_file(guard.dir / pid / "src" / "driver.c"));

    r = call("fw_sources", {{"project_id", pid}, {"remove", json::array({"src/startup.c"})}});
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
    CHECK(ev[0]["t_ms"].is_null());          // Renode hook prints carry no t
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
    CHECK(e2[0].find("unknown scenario fixture") == 0);
    auto [s3, e3] = load_scenario("{not json");
    CHECK(s3.is_null());
    CHECK(e3.size() == 1);
    auto [s4, e4] = load_scenario(json{{"schema", "wrong"}, {"tracks", json::array()}});
    CHECK(e4.size() >= 2);  // bad schema + empty tracks

    // Validation error text spot-checks (parity-sensitive)
    json bad_pin = {{"schema", "aicad.fwscenario/1"},
                    {"tracks", json::array({{{"kind", "gpio_edges"},
                                             {"pin", "P99"},
                                             {"events", json::array({{{"t_ms", 1}, {"level", 1}}})}}})}};
    auto errs = validate_scenario(bad_pin);
    REQUIRE(errs.size() == 1);
    CHECK(errs[0].find("pin must be STM32-style") != std::string::npos);

    // Plan: user_button on the stm32f4 platform → Button attach + 6 timeline actions
    json caps = get_capabilities("stm32f4");
    std::string platform_path = get_platform_file("stm32f4");
    REQUIRE(!platform_path.empty());
    std::ifstream in(platform_path);
    std::stringstream ss;
    ss << in.rdbuf();
    ScenarioPlan plan = build_scenario_plan(fx, caps, ss.str());
    REQUIRE(plan.errors.empty());
    CHECK(plan.setup_lines.size() == 1);  // one Button peripheral
    CHECK(plan.setup_lines[0].find("Miscellaneous.Button @ gpioPortA 0") != std::string::npos);
    CHECK(plan.timeline.size() == 6);     // 3 presses × (press+release)
    CHECK(plan.duration_ms == doctest::Approx(1080.0));

    // ADC track on nrf52 → honest refusal (no stub window registered)
    ScenarioPlan adc_plan = build_scenario_plan(adc, get_capabilities("nrf52"), "");
    REQUIRE(adc_plan.errors.size() == 1);
    CHECK(adc_plan.errors[0].find("adc stimulus unsupported on family 'nrf52'") == 0);

    // gpio stimulus on a platform without the port → honest refusal
    ScenarioPlan no_port = build_scenario_plan(fx, get_capabilities("rp2040"), "");
    CHECK(!no_port.errors.empty());

    // timeline rendering: sleep interleave + CHUDWARE_T_MARK marks
    auto lines = timeline_resc_lines({{100.0, "act1"}, {100.0, "act2"}, {250.0, "act3"}}, 1.0);
    REQUIRE(lines.size() >= 7);
    CHECK(lines[0] == "echo \"CHUDWARE_T_MARK 0\"");
    CHECK(lines[1] == "sleep 0.100");
    CHECK(lines[2] == "echo \"CHUDWARE_T_MARK 100\"");
    CHECK(lines[3] == "act1");
    CHECK(lines[4] == "act2");
    CHECK(lines[5] == "sleep 0.150");
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

TEST_CASE("fw_sim_run LIVE: QEMU semihosting path (honest skip without tools)") {
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
    // cortex-m4 → canonical cortex-m (generic platform). With NO assertions
    // the T-FIN2 gate has nothing to refuse, so the run takes the preferred
    // Renode backend even on the generic platform (same as the oracle).
    json r = call("fw_sim_run", {{"project_id", pid},
                                 {"elf_path", elf},
                                 {"timeout_s", 15.0}});
    CHECK(r.value("passed", false) == true);
    CHECK(r.value("ms", 0.0) > 0.0);
    REQUIRE_FALSE(r["trace_path"].is_null());
    json trace = read_json(r["trace_path"].get<std::string>());
    CHECK(trace.value("schema", "") == "aicad.firmware.trace/1");
    CHECK(trace.value("source", "") == "renode");
    CHECK(trace.contains("events"));

    // A uart: assertion exceeds the generic platform's capability → QEMU
    // semihosting fallback (T-FIN2 gate), which really launches QEMU on the
    // freshly-built ELF. NOTE (oracle gap, verified identical in Python):
    // this QEMU build writes semihosting output to STDERR and both engines
    // capture stdout only, so no UART text lands in uart_log — the assertion
    // fails the same way in both.
    r = call("fw_sim_run", {{"project_id", pid},
                            {"elf_path", elf},
                            {"assertions", json::array({"uart:hello world"})},
                            {"timeout_s", 15.0}});
    CHECK(r.value("passed", true) == false);
    CHECK(r["assertion_results"][0]["detail"].get<std::string>() ==
          "'hello world' not found in uart_log");
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
    json r = call("fw_sim_run", {{"project_id", pid},
                                 {"elf_path", elf},
                                 {"assertions", json::array({"uart:hello renode"})},
                                 {"timeout_s", 20.0}});
    INFO("uart_log: ", r["uart_log"].get<std::string>().substr(0, 600));
    CHECK(r.value("passed", false) == true);
    CHECK(r["uart_log"].get<std::string>().find("hello renode") != std::string::npos);
    REQUIRE_FALSE(r["trace_path"].is_null());
    json trace = read_json(r["trace_path"].get<std::string>());
    CHECK(trace.value("source", "") == "renode");
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

TEST_CASE("fw_verify_board: emulate=true takes the live path") {
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
        {"timeout_s", 15.0},
    });
    REQUIRE(r.value("ok", false) == true);
    INFO("dynamic: ", r["dynamic"].dump());
    // The live QEMU run really launched (this is the live path, not the
    // deferred degrade — the R4a first increment returned a "not ported yet"
    // reason here). On THIS box the oracle's QEMU capture gap (semihosting →
    // stderr) means no pin events come back, so BOTH engines report the same
    // honest degrade; the parity harness diffs this exact case.
    CHECK(r["dynamic"].value("ran", true) == false);
    CHECK(r["dynamic"].value("reason", "") == "emulation produced no pin events");
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
