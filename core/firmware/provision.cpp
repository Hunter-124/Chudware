#include "firmware/provision.h"

#include <algorithm>
#include <regex>
#include <sstream>
#include <string>
#include <vector>

#include "firmware/platform_db.h"
#include "firmware/subprocess.h"

namespace chudware::firmware {
namespace {

constexpr double kVersionTimeoutS = 5.0;

struct ToolDescriptor {
    std::vector<std::string> aliases;
    std::vector<std::string> version_args;
    std::vector<std::string> unlocks;
    std::string install_hint;
};

// Mirrors provision.FW_TOOL_DESCRIPTORS (insertion order matters for JSON
// key order parity — nlohmann::json sorts keys, so only content matters).
const std::vector<std::pair<std::string, ToolDescriptor>> kToolDescriptors = {
    {"cmake", {{"cmake"}, {"--version"}, {"build:cortex-m"},
               "Install CMake: `sudo dnf install cmake` (or `sudo apt install cmake`)."}},
    {"ninja", {{"ninja"}, {"--version"}, {"build:cortex-m (faster generator; optional)"},
               "Install Ninja: `sudo dnf install ninja-build` (or `sudo apt install ninja-build`)."}},
    {"arm-none-eabi-gcc", {{"arm-none-eabi-gcc"}, {"--version"}, {"build:cortex-m"},
                           "Install the ARM cross-toolchain: `sudo dnf install arm-none-eabi-gcc-cs "
                           "arm-none-eabi-newlib` (or `sudo apt install gcc-arm-none-eabi`)."}},
    {"renode", {{"renode"}, {"--version"},
                {"emulate:cortex-m (stm32f4/stm32f1/nrf52 full, rp2040 minimal)"},
                "Install Renode from https://renode.io/#downloads (portable tarball or "
                "`sudo dnf copr enable ...`); put `renode` on PATH."}},
    {"qemu-system-arm", {{"qemu-system-arm"}, {"--version"},
                         {"emulate:cortex-m (fallback when Renode absent)"},
                         "Install QEMU ARM: `sudo dnf install qemu-system-arm` (or `sudo apt install qemu-system-arm`)."}},
    {"probe-rs", {{"probe-rs"}, {"--version"}, {"flash:cortex-m", "debug:cortex-m"},
                  "Install probe-rs: `cargo install probe-rs-tools` (MIT/Apache-2.0)."}},
    {"esptool", {{"esptool.py", "esptool"}, {"version"}, {"flash:esp32"},
                 "Install esptool: `pip install esptool` (GPL-2.0+, subprocess-only)."}},
    {"idf.py", {{"idf.py"}, {"--version"}, {"build:esp32"},
                "Install ESP-IDF (Apache-2.0): https://docs.espressif.com/projects/esp-idf/ "
                "then `. ./export.sh` so idf.py is on PATH."}},
    {"arduino-cli", {{"arduino-cli"}, {"version"}, {"build:avr"},
                     "Install arduino-cli: https://arduino.github.io/arduino-cli/ (GPL-3, subprocess-only)."}},
    {"avrdude", {{"avrdude"}, {"-v"}, {"flash:avr"},
                 "Install avrdude: `sudo dnf install avrdude` (or `sudo apt install avrdude`)."}},
    {"openocd", {{"openocd"}, {"--version"}, {"debug:cortex-m (via gdb/MI)"},
                 "Install OpenOCD: `sudo dnf install openocd` (or `sudo apt install openocd`)."}},
    {"arm-none-eabi-gdb", {{"arm-none-eabi-gdb"}, {"--version"}, {"debug:cortex-m (via gdb/MI)"},
                           "Install the ARM gdb: `sudo dnf install arm-none-eabi-gdb` (or "
                           "`sudo apt install gdb-multiarch` as a cross-capable fallback)."}},
    {"avr-gdb", {{"avr-gdb"}, {"--version"}, {"debug:avr (via simavr + gdb/MI)"},
                "Install avr-gdb: `sudo dnf install avr-gdb` (or `sudo apt install gdb-avr`)."}},
    {"simavr", {{"simavr"}, {"--version"}, {"emulate:avr", "debug:avr (via simavr + gdb/MI)"},
               "Install simavr: `sudo dnf install simavr` (or `sudo apt install simavr`); "
               "GPL/LGPL dual-licensed per-file, subprocess-only here."}},
};

// MCU family capability rules (mirrors provision.FW_FAMILY_RULES).
struct FamilyRule {
    std::vector<std::string> all_of;
    std::vector<std::string> any_of;
};
const std::vector<std::pair<std::string, std::vector<std::pair<std::string, FamilyRule>>>>
    kFamilyRules = {
        {"cortex-m",
         {{"can_build", {{"cmake", "arm-none-eabi-gcc"}, {}}},
          {"can_emulate", {{}, {"renode", "qemu-system-arm"}}},
          {"can_flash", {{"probe-rs"}, {}}},
          {"can_debug_gdb", {{"openocd", "arm-none-eabi-gdb"}, {}}}}},
        {"esp32",
         {{"can_build", {{"idf.py"}, {}}},
          {"can_emulate", {{}, {}}},  // no emulation path for ESP32 (design decision)
          {"can_flash", {{"esptool"}, {}}}}},
        {"avr",
         {{"can_build", {{"arduino-cli"}, {}}},
          {"can_emulate", {{}, {"simavr"}}},
          {"can_flash", {{"avrdude"}, {}}},
          {"can_debug_gdb", {{"simavr", "avr-gdb"}, {}}}}},
};

// The 8 core tools that drive `missing` + banners.
const char* kCoreTools[] = {"cmake", "ninja", "arm-none-eabi-gcc", "renode",
                            "qemu-system-arm", "probe-rs", "esptool", "avrdude"};

const ToolDescriptor* descriptor_for(const std::string& name) {
    for (const auto& [n, d] : kToolDescriptors)
        if (n == name) return &d;
    return nullptr;
}

std::string which_tool(const std::string& name) {
    const ToolDescriptor* d = descriptor_for(name);
    if (!d) return "";
    for (const auto& alias : d->aliases) {
        std::string path = which(alias);
        if (!path.empty()) return path;
    }
    return "";
}

const std::regex kVersionRe(R"(\d+\.\d+(?:\.\d+)?(?:[-+~][\w.]+)?)");

// Best-effort `<tool> --version` → short version string. Never throws.
nlohmann::json probe_version(const std::string& path, const std::vector<std::string>& args) {
    std::vector<std::string> argv = {path};
    argv.insert(argv.end(), args.begin(), args.end());
    RunResult rr = run_capture(argv, "", kVersionTimeoutS);
    std::string text = rr.stdout_text + rr.stderr_text;
    std::string first;
    std::string line;
    std::istringstream in(text);
    while (std::getline(in, line)) {
        size_t b = line.find_first_not_of(" \t\r");
        if (b != std::string::npos) {
            size_t e = line.find_last_not_of(" \t\r");
            first = line.substr(b, e - b + 1);
            break;
        }
    }
    if (first.empty()) return nullptr;
    std::smatch m;
    if (std::regex_search(first, m, kVersionRe)) return m.str();
    return first.substr(0, 80);
}

}  // namespace

nlohmann::json fw_status() {
    nlohmann::json tools = nlohmann::json::object();
    for (const auto& [name, d] : kToolDescriptors) {
        std::string path = which_tool(name);
        tools[name] = {
            {"found", !path.empty()},
            {"path", path.empty() ? nlohmann::json(nullptr) : nlohmann::json(path)},
            {"version", path.empty() ? nlohmann::json(nullptr) : probe_version(path, d.version_args)},
            {"install_hint", d.install_hint},
            {"unlocks", d.unlocks},
        };
    }

    auto found = [&tools](const std::string& n) {
        return tools.contains(n) && tools[n].value("found", false);
    };

    nlohmann::json families = nlohmann::json::object();
    for (const auto& [family, caps] : kFamilyRules) {
        nlohmann::json entry = nlohmann::json::object();
        std::vector<std::string> missing;
        for (const auto& [cap, rule] : caps) {
            bool ok = false;
            if (!rule.all_of.empty()) {
                ok = true;
                for (const auto& t : rule.all_of) ok = ok && found(t);
            }
            if (!rule.any_of.empty()) {
                for (const auto& t : rule.any_of) ok = ok || found(t);
            }
            // capability with no provisioned path → false (empty all_of + any_of)
            entry[cap] = ok;
            if (!ok) {
                for (const auto& t : rule.all_of)
                    if (!found(t)) missing.push_back(t);
                for (const auto& t : rule.any_of)
                    if (!found(t)) missing.push_back(t);
            }
        }
        std::sort(missing.begin(), missing.end());
        missing.erase(std::unique(missing.begin(), missing.end()), missing.end());
        entry["missing"] = missing;
        families[family] = entry;
    }

    nlohmann::json missing_core = nlohmann::json::array();
    for (const char* t : kCoreTools)
        if (!found(t)) missing_core.push_back(t);

    nlohmann::json banners = nlohmann::json::array();
    if (!families["cortex-m"].value("can_build", false))
        banners.push_back("Cortex-M builds unavailable: install cmake + arm-none-eabi-gcc "
                          "(see tools[*].install_hint).");
    if (!families["cortex-m"].value("can_emulate", false))
        banners.push_back("Firmware emulation unavailable: install Renode (preferred) or qemu-system-arm.");
    if (!families["cortex-m"].value("can_flash", false))
        banners.push_back("Cortex-M flashing unavailable: `cargo install probe-rs-tools`.");
    if (!families["esp32"].value("can_flash", false))
        banners.push_back("ESP32 flashing unavailable: `pip install esptool`.");
    if (!families["avr"].value("can_flash", false))
        banners.push_back("AVR flashing unavailable: install avrdude "
                          "(`sudo dnf install avrdude` / `sudo apt install avrdude`).");

    return {
        {"ok", true},
        {"tools", tools},
        {"families", families},
        {"platforms", capability_matrix()},
        {"banners", banners},
        {"missing", missing_core},
    };
}

}  // namespace chudware::firmware
