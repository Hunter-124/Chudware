// Renode and QEMU emulator wrappers (T-RF15 port of
// engine_oracle/firmware/emulator.py). Both backends are subprocess-only,
// never linked; a missing binary is graceful (passed=false, uart_log carries
// the reason), never a throw. See contracts/firmware_rpc.md §4/§7/§9.
#pragma once

#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::firmware {

// contracts/firmware_rpc.md §4 (+ §7.1 gpio_events/trace_path).
struct SimResult {
    bool passed = false;
    std::string uart_log;
    nlohmann::json gpio_states = nlohmann::json::object();
    nlohmann::json assertion_results = nlohmann::json::array();
    double ms = 0.0;
    nlohmann::json gpio_events = nlohmann::json::array();
    std::optional<std::string> trace_path;
    nlohmann::json bus_events = nlohmann::json::array();
    nlohmann::json bus_summary = nlohmann::json::object();
    nlohmann::json uart_events = nlohmann::json::array();

    nlohmann::json to_dict() const;
};

// ── pure parsing (no I/O) ───────────────────────────────────────────────────

// CHUDWARE_GPIO_EVT lines → [{"type","seq","t_ms","pin","level"}] (t_ms null when
// the emitting side had no timestamp). Bounded to 4096 ordered entries; over-limit
// streams retain head+tail with a trace_truncated diagnostic entry.
nlohmann::json parse_gpio_events(const std::string& text);

// Non-marker, non-trace lines → [{"type":"uart","seq","t_ms","text"}],
// time-stamped from CHUDWARE_T_MARK markers and bounded like GPIO events.
nlohmann::json parse_uart_events(const std::string& text);

// Precise model-origin CHUDWARE_BUS_EVT evidence, bounded to 4096 events.
nlohmann::json parse_bus_events(const std::string& text);
// Renode ODR snapshot blocks → {"PA0": 0, ..., "PA15": 0}.
nlohmann::json parse_renode_gpio_odr(const std::string& text);

// Final pin → level: last event per pin, overridden by the ODR snapshot.
nlohmann::json derive_gpio_states(const nlohmann::json& events,
                                  const nlohmann::json& odr_states);

// (port_letter, pin_number) pairs referenced by gpio:/toggles:/period:
// assertions — these get Renode state-change hooks + ODR reads.
std::vector<std::pair<char, int>> pins_from_assertions(
    const std::vector<std::string>& assertions);

// Write the bounded aicad.firmware.trace/1 artifact next to the ELF (atomic
// tmp+replace; null on any failure or when the ELF does not exist). The additive
// `truncated` field discloses event-limit clipping.
std::optional<std::string> write_trace_artifact(const std::string& elf_path,
                                                const std::string& source,
                                                const std::string& uart_log,
                                                const nlohmann::json& gpio_events,
                                                const nlohmann::json& gpio_states);

// Pure assertion evaluator (see emulator.py::evaluate_assertions for the
// grammar). Returns [{name, passed, detail}] in assertion order.
nlohmann::json evaluate_assertions(const std::string& uart_log,
                                   const nlohmann::json& gpio_states,
                                   const std::vector<std::string>& assertions,
                                   const nlohmann::json& gpio_events,
                                   const nlohmann::json& uart_events);

// Generate the Renode .resc script (see emulator.py::_build_resc).
std::string build_resc(const std::string& elf_path, const std::string& platform_file,
                       double run_seconds,
                       const std::vector<std::pair<char, int>>& gpio_pins,
                       const nlohmann::json& gpio_regmap,  // null → STM32F4 default
                       const std::vector<std::string>& scenario_setup_lines,
                       const std::vector<std::pair<double, std::string>>& scenario_timeline);

// Top-level entry (emulator.py::run_simulation): pick Renode (with matching
// platform) or the QEMU UART fallback, without semihosting host services.
// `scenario` is a validated aicad.fwscenario/1 dict or null.
SimResult run_simulation(const std::string& elf_path, const std::string& mcu_family,
                         const std::vector<std::string>& assertions, double timeout_s = 30.0,
                         const nlohmann::json& scenario = nlohmann::json(),
                         double run_s = 0.0, const std::string& backend = "auto");

}  // namespace chudware::firmware
