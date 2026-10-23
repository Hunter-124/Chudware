#include "firmware/emulator.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <regex>
#include <set>
#include <sstream>

#include "firmware/platform_db.h"
#include "firmware/pyfmt.h"
#include "firmware/scenario.h"
#include "firmware/subprocess.h"
#include "firmware/bus_models.h"
#include "firmware/hardware.h"
#include "firmware/private_storage.h"
#include "firmware/renode_encoding.h"
#include <map>

namespace chudware::firmware {
namespace {

constexpr size_t kUartCap = 32000;
constexpr size_t kEventCap = 4096;

struct EmulatorInputs {
    std::filesystem::path dir;
    explicit EmulatorInputs(const std::string& prefix) : dir(private_temp_directory(prefix)) {}
    ~EmulatorInputs() {
        std::error_code ec;
        std::filesystem::remove_all(dir, ec);
    }
    std::string copy(const std::string& source, const std::string& name) {
        const auto destination = dir / name;
        private_storage_detail::atomic_write(destination, private_storage_detail::read_file(source));
        return destination.string();
    }
};

// CHUDWARE_GPIO_EVT <PIN>=<level>[ t=<num>[ms|us|s]] — target-emitted events only.
const std::regex kGpioEvtRe(
    R"(CHUDWARE_GPIO_EVT\s+([A-Za-z_][A-Za-z0-9_]*)=([01])(?:\s+t=([0-9]+(?:\.[0-9]+)?)(ms|us|s)?)?)");
const std::regex kOdrBeginRe(R"(CHUDWARE_GPIO_ODR_BEGIN\s+([A-K]))");
const std::regex kOdrEndRe(R"(CHUDWARE_GPIO_ODR_END\s+([A-K]))");
const std::regex kHexRe(R"(0x[0-9a-fA-F]+)");
const std::regex kTMarkRe(R"(CHUDWARE_T_MARK\s+([0-9]+(?:\.[0-9]+)?))");
const std::regex kStm32PinRe(R"(^P([A-K])(\d{1,2})$)");
const std::regex kReplUartRe(R"(^(\w+)\s*:\s*(?:UART\.|Chudware\.TimedSTM32UART))");

// Assertion-grammar regexes (emulator.py).
const std::regex kGpioTimeRe(R"(^([^=@]+)=(\S+?)@([<>]?)([0-9]+(?:\.[0-9]+)?)ms$)");
const std::regex kTogglesRe(R"(^([A-Za-z_][A-Za-z0-9_]*)\s*(>=|<=|==|>|<)\s*(\d+)$)");
const std::regex kPeriodRe(
    "^(?:([A-Za-z_][A-Za-z0-9_]*))\\s*=\\s*([0-9]+(?:\\.[0-9]+)?)ms"
    "\\s*(?:\xC2\xB1|\\+-)\\s*([0-9]+(?:\\.[0-9]+)?)ms$");  // ± or +-
const std::regex kUartWithinRe(R"(^(.+)@([0-9]+(?:\.[0-9]+)?)ms$)");

double to_ms(double t, const std::string& unit) {
    if (unit == "s") return t * 1000.0;
    if (unit == "us") return t / 1000.0;
    return t;
}

std::string cap(const std::string& text) {
    if (text.size() <= kUartCap) return text;
    return text.substr(0, kUartCap) + "\n\xE2\x80\xA6[truncated]";
}

std::vector<std::string> split_lines(const std::string& text) {
    std::vector<std::string> out;
    std::istringstream in(text);
    std::string line;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        out.push_back(line);
    }
    return out;
}

nlohmann::json bound_events(const nlohmann::json& events) {
    if (!events.is_array() || events.size() <= kEventCap) return events;
    constexpr size_t kFirst = (kEventCap - 1) / 2;
    constexpr size_t kLast = kEventCap - 1 - kFirst;
    nlohmann::json out = nlohmann::json::array();
    for (size_t i = 0; i < kFirst; ++i) out.push_back(events[i]);
    out.push_back({
        {"type", "trace_truncated"},
        {"seq", events[kFirst].value("seq", static_cast<long long>(kFirst))},
        {"t_ms", nullptr},
        {"dropped", events.size() - kFirst - kLast},
    });
    for (size_t i = events.size() - kLast; i < events.size(); ++i) out.push_back(events[i]);
    return out;
}

bool events_truncated(const nlohmann::json& events) {
    if (!events.is_array()) return false;
    return std::any_of(events.begin(), events.end(), [](const nlohmann::json& event) {
        return event.value("type", std::string()) == "trace_truncated";
    });
}

std::string trim(const std::string& s) {
    size_t b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos) return "";
    return s.substr(b, s.find_last_not_of(" \t\r\n") - b + 1);
}

std::optional<nlohmann::json> parse_bus_event(const std::string& line, size_t seq) {
    const size_t marker = line.find("CHUDWARE_BUS_EVT ");
    if (marker == std::string::npos) return std::nullopt;
    std::istringstream in(line.substr(marker + 17));
    std::string token;
    nlohmann::json event = {{"type", "bus"}, {"seq", seq}};
    while (in >> token) {
        const auto separator = token.find('=');
        if (separator == std::string::npos) continue;
        const auto key = token.substr(0, separator), value = token.substr(separator + 1);
        if (key == "t_ns") {
            try {
                event[key] = std::stoull(value);
                event["t_ms"] = event[key].get<double>() / 1e6;
            } catch (...) {
                event[key] = nullptr;
            }
        } else {
            event[key] = value;
        }
    }
    if (event.value("event", std::string()) == "trace_truncated")
        event["type"] = "trace_truncated";
    return event;
}

std::map<std::string, std::string> uart_streams(const nlohmann::json& events) {
    std::map<std::string, std::string> streams;
    for (const auto& event : events) {
        if (event.value("type", std::string()) == "trace_truncated") {
            for (auto& [bus, stream] : streams) stream += "\n[trace truncated]\n";
        } else if (event.contains("t_ns")) {
            streams[event.value("bus", std::string())] += event.value("text", std::string());
        }
    }
    return streams;
}


nlohmann::json assertion_result(const std::string& name, bool passed,
                                const std::string& detail) {
    return {{"name", name}, {"passed", passed}, {"detail", detail}};
}

SimResult refused_result(const std::string& reason,
                         const std::vector<std::string>& assertions,
                         std::chrono::steady_clock::time_point t0) {
    SimResult r;
    r.passed = false;
    r.uart_log = reason;
    const std::string summary = reason.substr(0, reason.find('\n'));
    for (const auto& assertion : assertions)
        r.assertion_results.push_back(
            assertion_result(assertion, false, "not evaluated: " + summary));
    r.ms = std::chrono::duration<double, std::milli>(
               std::chrono::steady_clock::now() - t0)
               .count();
    return r;
}

std::string image_error(const std::string& elf_path, unsigned expected_machine) {
    namespace fs = std::filesystem;
    std::error_code ec;
    if (elf_path.empty()) return "invalid_image: ELF path is empty";
    if (!fs::is_regular_file(elf_path, ec))
        return "invalid_image: '" + elf_path + "' is not a regular file";
    std::ifstream in(elf_path, std::ios::binary);
    unsigned char header[20] = {};
    in.read(reinterpret_cast<char*>(header), sizeof(header));
    if (in.gcount() != static_cast<std::streamsize>(sizeof(header)) ||
        header[0] != 0x7f || header[1] != 'E' || header[2] != 'L' || header[3] != 'F')
        return "invalid_image: '" + elf_path + "' is not an ELF image";
    if ((header[4] != 1 && header[4] != 2) ||
        (header[5] != 1 && header[5] != 2) || header[6] != 1)
        return "invalid_image: '" + elf_path + "' has an invalid ELF identification header";
    const auto u16 = [&](size_t offset) {
        if (header[5] == 1)
            return static_cast<unsigned>(header[offset]) |
                   (static_cast<unsigned>(header[offset + 1]) << 8);
        return (static_cast<unsigned>(header[offset]) << 8) |
               static_cast<unsigned>(header[offset + 1]);
    };
    const unsigned type = u16(16);
    const unsigned machine = u16(18);
    if ((type != 2 && type != 3) || machine == 0)
        return "invalid_image: '" + elf_path + "' is not an executable ELF image";
    if (machine != expected_machine)
        return "invalid_image: '" + elf_path + "' targets ELF machine " +
               std::to_string(machine) + ", expected " + std::to_string(expected_machine);
    const auto minimum_size = header[4] == 1 ? 52ULL : 64ULL;
    if (fs::file_size(elf_path, ec) < minimum_size || ec)
        return "invalid_image: '" + elf_path + "' has a truncated ELF header";
    return "";
}

bool valid_stm32_pin(const std::string& pin) {
    std::smatch m;
    return std::regex_match(pin, m, kStm32PinRe) && std::stoi(m[2].str()) <= 15;
}

bool finite_number(const std::string& text, bool positive) {
    try {
        const double value = std::stod(text);
        return std::isfinite(value) && (positive ? value > 0.0 : value >= 0.0);
    } catch (...) {
        return false;
    }
}

bool bounded_integer(const std::string& text) {
    try {
        (void)std::stol(text);
        return true;
    } catch (...) {
        return false;
    }
}

std::vector<std::string> assertion_errors(const std::vector<std::string>& assertions) {
    std::vector<std::string> errors;
    for (const auto& raw : assertions) {
        const std::string assertion = trim(raw);
        std::string pin;
        if (assertion.empty()) {
            errors.push_back("assertion must not be empty");
            continue;
        }
        if (assertion.rfind("gpio:", 0) == 0) {
            const std::string spec = trim(assertion.substr(5));
            std::smatch m;
            if (std::regex_match(spec, m, kGpioTimeRe)) {
                pin = trim(m[1].str());
                const std::string level = trim(m[2].str());
                if (level != "0" && level != "1")
                    errors.push_back("'" + assertion + "' has invalid logic level '" + level + "'");
                if (!finite_number(m[4].str(), false))
                    errors.push_back("'" + assertion + "' has an invalid event time");
            } else {
                const size_t eq = spec.find('=');
                if (eq == std::string::npos || spec.find('=', eq + 1) != std::string::npos) {
                    errors.push_back("'" + assertion + "' must be gpio:PIN=0 or gpio:PIN=1");
                    continue;
                }
                pin = trim(spec.substr(0, eq));
                const std::string level = trim(spec.substr(eq + 1));
                if (level != "0" && level != "1")
                    errors.push_back("'" + assertion + "' has invalid logic level '" + level + "'");
            }
        } else if (assertion.rfind("toggles:", 0) == 0) {
            const std::string spec = trim(assertion.substr(8));
            std::smatch m;
            if (!std::regex_match(spec, m, kTogglesRe)) {
                errors.push_back("'" + assertion + "' has invalid toggle assertion syntax");
                continue;
            }
            pin = m[1].str();
            if (!bounded_integer(m[3].str()))
                errors.push_back("'" + assertion + "' has an out-of-range toggle count");
        } else if (assertion.rfind("period:", 0) == 0) {
            const std::string spec = trim(assertion.substr(7));
            std::smatch m;
            if (!std::regex_match(spec, m, kPeriodRe)) {
                errors.push_back("'" + assertion + "' has invalid period assertion syntax");
                continue;
            }
            pin = m[1].str();
            if (!finite_number(m[2].str(), true) || !finite_number(m[3].str(), false))
                errors.push_back("'" + assertion +
                                 "' requires finite positive period and non-negative tolerance");
        } else if (assertion.rfind("uart_within:", 0) == 0) {
            const std::string spec = trim(assertion.substr(12));
            std::smatch m;
            if (!std::regex_match(spec, m, kUartWithinRe) ||
                trim(m[1].str()).empty() || !finite_number(m[2].str(), false))
                errors.push_back("'" + assertion + "' has invalid timed UART assertion syntax");
        } else if (assertion.rfind("uart:", 0) == 0 && assertion.size() == 5) {
            errors.push_back("uart assertion text must not be empty");
        }
        if (!pin.empty() && !valid_stm32_pin(pin))
            errors.push_back("'" + assertion + "' uses invalid pin '" + pin +
                             "' (expected PA0..PK15)");
    }
    return errors;
}

bool requires_timestamped_gpio_observation(const std::string& raw) {
    const std::string assertion = trim(raw);
    if (assertion.rfind("toggles:", 0) == 0 || assertion.rfind("period:", 0) == 0)
        return true;
    if (assertion.rfind("gpio:", 0) != 0) return false;
    const std::string spec = trim(assertion.substr(5));
    std::smatch match;
    return std::regex_match(spec, match, kGpioTimeRe);
}


int count_toggles(const nlohmann::json& gpio_events, const std::string& pin) {
    std::vector<int> levels;
    for (const auto& e : gpio_events)
        if (e.value("pin", std::string()) == pin) levels.push_back(e.value("level", 0));
    int n = 0;
    for (size_t i = 1; i < levels.size(); ++i)
        if (levels[i] != levels[i - 1]) ++n;
    return n;
}

nlohmann::json final_gpio_states(const std::string& text,
                                 const nlohmann::json& odr_states) {
    nlohmann::json states = nlohmann::json::object();
    std::istringstream lines(text);
    std::string line;
    while (std::getline(lines, line)) {
        std::smatch match;
        if (std::regex_search(line, match, kGpioEvtRe))
            states[match[1].str()] = std::stoi(match[2].str());
    }
    if (odr_states.is_object())
        for (auto& [pin, level] : odr_states.items()) states[pin] = level;
    return states;
}

// Renode backend (emulator.py::RenodeEmulator.run).
SimResult run_renode(const std::string& elf_path, const std::string& platform_file,
                     const std::vector<std::string>& assertions, double timeout_s,
                     const nlohmann::json& gpio_regmap, const nlohmann::json& scenario,
                     const nlohmann::json& mcu_caps, double requested_run_s) {
    auto t0 = std::chrono::steady_clock::now();
    if (which("renode").empty())
        return refused_result(
            "emulator_not_found: 'renode' is not installed / not on PATH", assertions, t0);

    double run_seconds = requested_run_s > 0 ? requested_run_s : 1.0;
    auto gpio_pins = pins_from_assertions(assertions);
    EmulatorInputs inputs("renode");
    const auto copied_elf = inputs.copy(elf_path, "firmware.elf");
    const auto copied_platform = inputs.copy(platform_file, "platform.repl");

    // Compile the scenario — pure plan first, then aux files on disk.
    std::vector<std::string> scenario_setup;
    std::vector<std::pair<double, std::string>> scenario_timeline;
    std::string scenario_dir;
    nlohmann::json resolved_links=nlohmann::json::array();
    nlohmann::json limits=nlohmann::json::array();
    if (scenario.is_object() || mcu_caps.value("timed_bus",false)) {
        nlohmann::json caps = mcu_caps.is_object() ? mcu_caps : get_capabilities("cortex-m");
        std::string platform_text;
        {
            std::ifstream in(copied_platform);
            std::stringstream ss;
            ss << in.rdbuf();
            platform_text = ss.str();
        }
        ScenarioPlan plan;
        if(scenario.is_object()) plan=build_scenario_plan(scenario,caps,platform_text,requested_run_s * 1000);
        else {
            plan.aux_files["chudware_buses.cs"]=generated_bus_source(nlohmann::json::array());
            plan.pre_platform_lines={"include @{CHUDWARE_SCENARIO_DIR}/chudware_buses.cs"};
            plan.warnings={"Functional bus mode: reset 16MHz HSI clocks; no calibrated electrical network"};
        }
        if(requested_run_s>0 && plan.duration_ms>requested_run_s*1000)
            plan.errors.push_back("scenario stimulus exceeds explicit run_s virtual horizon");
        if (!plan.errors.empty()) {
            SimResult r;
            r.passed = false;
            std::string joined;
            for (const auto& e : plan.errors) {
                if (!joined.empty()) joined += "; ";
                joined += e;
            }
            r.uart_log = "scenario_error: " + joined;
            for (const auto& a : assertions)
                r.assertion_results.push_back(assertion_result(
                    a, false, "not evaluated: scenario invalid (see uart_log)"));
            r.ms = std::chrono::duration<double, std::milli>(
                       std::chrono::steady_clock::now() - t0)
                       .count();
            return r;
        }
        auto dir = private_directory(inputs.dir / "scenario");
        scenario_dir = dir.string();
        materialize_aux_files(plan, scenario_dir);
        scenario_setup = plan.pre_platform_lines;
        scenario_setup.insert(scenario_setup.end(),plan.setup_lines.begin(),plan.setup_lines.end());
        scenario_timeline = plan.timeline;
        resolved_links=plan.resolved_links;
        for(const auto& warning:plan.warnings) limits.push_back(warning);
        if(requested_run_s==0) run_seconds=std::max(run_seconds,plan.duration_ms/1000+1.0);
    }

    std::string resc = build_resc(copied_elf, copied_platform, run_seconds, gpio_pins, gpio_regmap,
                                  scenario_setup, scenario_timeline);
    namespace fs = std::filesystem;
    fs::path resc_path = inputs.dir / "simulation.resc";
    private_storage_detail::atomic_write(resc_path, resc);

    std::vector<std::string> cmd = {"renode", "--disable-xwt", "--console", resc_path.string()};
    RunResult rr = run_capture(cmd, inputs.dir.string(), timeout_s, true);

    if (rr.not_found)
        return refused_result(
            "emulator_not_found: 'renode' is not installed / not on PATH", assertions, t0);
    if (rr.timed_out)
        return refused_result(
            "timeout: renode exceeded " + fmt_g(timeout_s) + "s", assertions, t0);
    if (rr.output_limit_exceeded)
        return refused_result("output_limit_exceeded: Renode capture is incomplete", assertions, t0);
    std::string raw = rr.stdout_text;
    if (!rr.stderr_text.empty()) {
        if (!raw.empty() && raw.back() != '\n') raw += '\n';
        raw += rr.stderr_text;
    }

    std::string uart_log = cap(raw);
    nlohmann::json gpio_events = parse_gpio_events(raw);
    nlohmann::json gpio_states = final_gpio_states(raw, parse_renode_gpio_odr(raw));
    nlohmann::json uart_events = parse_uart_events(raw);
    std::string firmware_output=raw;
    if(mcu_caps.value("timed_bus",false)) {
        const auto streams = uart_streams(uart_events);
        firmware_output.clear();for(const auto& [bus,stream]:streams) {firmware_output+=stream;firmware_output+='\n';}
        uart_log=cap(firmware_output+"\n"+raw);
    }
    auto trace_path = write_trace_artifact(elf_path, "renode", raw, gpio_events, gpio_states);
    SimResult r;
    r.bus_events=parse_bus_events(raw);
    r.uart_events=uart_events;
    r.bus_summary={{"resolved_links",resolved_links},{"limitations",limits},{"event_count",r.bus_events.size()},{"truncated",events_truncated(r.bus_events)}};
    if (rr.returncode != 0) {
        auto evidence=std::move(r.bus_events);
        auto summary=std::move(r.bus_summary);
        auto uart_evidence=std::move(r.uart_events);
        r = refused_result(
            cap("emulator_fault: renode exited with code " +
                std::to_string(rr.returncode) + "\n" + raw),
            assertions, t0);
        r.bus_events=std::move(evidence);r.bus_summary=std::move(summary);r.uart_events=std::move(uart_evidence);
        r.gpio_states = gpio_states;
        r.gpio_events = gpio_events;
        r.trace_path = trace_path;
        return r;
    }
    r.assertion_results =
        evaluate_assertions(firmware_output, gpio_states, assertions, gpio_events, uart_events);
    r.passed = true;
    for (const auto& a : r.assertion_results)
        if (!a.value("passed", false)) {
            r.passed = false;
            break;
        }
    r.uart_log = uart_log;
    r.gpio_states = gpio_states;
    r.ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    r.gpio_events = gpio_events;
    r.trace_path = trace_path;
    return r;
}

// QEMU fallback (emulator.py::QEMUEmulator.run).
SimResult run_qemu(const std::string& elf_path, double timeout_s,
                   const std::vector<std::string>& assertions) {
    auto t0 = std::chrono::steady_clock::now();
    if (which("qemu-system-arm").empty())
        return refused_result(
            "emulator_not_found: 'qemu-system-arm' is not installed / not on PATH",
            assertions, t0);
    EmulatorInputs inputs("qemu");
    const auto copied_elf = inputs.copy(elf_path, "firmware.elf");
    std::vector<std::string> cmd = {
        "qemu-system-arm", "-M", "mps2-an385", "-cpu", "cortex-m3",
        "-kernel", copied_elf, "-nographic", "-no-reboot",
    };
    RunResult rr = run_capture(cmd, inputs.dir.string(), timeout_s, true);
    if (rr.not_found)
        return refused_result(
            "emulator_not_found: 'qemu-system-arm' is not installed / not on PATH",
            assertions, t0);
    if (rr.timed_out)
        return refused_result(
            "timeout: qemu-system-arm exceeded " + fmt_g(timeout_s) + "s", assertions, t0);
    if (rr.output_limit_exceeded)
        return refused_result("output_limit_exceeded: QEMU capture is incomplete", assertions, t0);
    std::string raw = rr.stdout_text;
    if (!rr.stderr_text.empty()) {
        if (!raw.empty() && raw.back() != '\n') raw += '\n';
        raw += rr.stderr_text;
    }
    std::string uart_log = cap(raw);
    nlohmann::json gpio_events = parse_gpio_events(raw);
    nlohmann::json gpio_states = final_gpio_states(raw, nlohmann::json::object());
    auto trace_path = write_trace_artifact(elf_path, "qemu", raw, gpio_events, gpio_states);
    SimResult r;
    if (rr.returncode != 0) {
        r = refused_result(
            cap("emulator_fault: qemu-system-arm exited with code " +
                std::to_string(rr.returncode) + "\n" + raw),
            assertions, t0);
        r.gpio_states = gpio_states;
        r.gpio_events = gpio_events;
        r.trace_path = trace_path;
        return r;
    }
    r.assertion_results = evaluate_assertions(
        raw, gpio_states, assertions, gpio_events, parse_uart_events(raw));
    r.passed = true;
    for (const auto& a : r.assertion_results)
        if (!a.value("passed", false)) {
            r.passed = false;
            break;
        }
    r.uart_log = uart_log;
    r.gpio_states = gpio_states;
    r.ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    r.gpio_events = gpio_events;
    r.trace_path = trace_path;
    return r;
}

std::string assertion_capability(const std::string& assertion) {
    std::string a = trim(assertion);
    if (a.rfind("gpio:", 0) == 0 || a.rfind("toggles:", 0) == 0 || a.rfind("period:", 0) == 0)
        return "gpio";
    return "uart";
}

SimResult unsupported_result(const nlohmann::json& caps,
                             const std::vector<std::string>& assertions,
                             const std::set<std::string>& unsupported,
                             const std::string& reason,
                             std::chrono::steady_clock::time_point t0) {
    SimResult r;
    std::string file = caps.value("file", std::string());
    std::string family = caps.value("family", std::string());
    std::string notes = caps.value("notes", std::string());
    for (const auto& a : assertions) {
        if (unsupported.count(a))
            r.assertion_results.push_back(assertion_result(
                a, false, "unsupported on platform '" + file + "' (family " + family + "): " +
                              reason + " — " + notes));
        else
            r.assertion_results.push_back(assertion_result(
                a, false, "not evaluated: run refused (see unsupported assertions)"));
    }
    r.passed = false;
    r.uart_log = "platform_capability: " + reason + " (family " + family + ", platform " + file +
                 "). " + notes;
    r.ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    return r;
}

}  // namespace

nlohmann::json SimResult::to_dict() const {
    return {
        {"passed", passed},
        {"uart_log", uart_log},
        {"gpio_states", gpio_states},
        {"assertion_results", assertion_results},
        {"ms", std::round(ms * 100.0) / 100.0},
        {"gpio_events", gpio_events},
        {"bus_events", bus_events},
        {"bus_summary", bus_summary},
        {"uart_events", uart_events},
        {"trace_path", trace_path ? nlohmann::json(*trace_path) : nlohmann::json(nullptr)},
    };
}

nlohmann::json parse_bus_events(const std::string& text) {
    nlohmann::json events=nlohmann::json::array();
    for (const auto& line : split_lines(text)) {
        auto event = parse_bus_event(line, events.size());
        if (event) events.push_back(std::move(*event));
    }
    return bound_events(events);
}

nlohmann::json parse_gpio_events(const std::string& text) {
    nlohmann::json events = nlohmann::json::array();
    auto lines = split_lines(text);
    for (size_t i = 0; i < lines.size(); ++i) {
        std::smatch m;
        if (!std::regex_search(lines[i], m, kGpioEvtRe)) continue;
        nlohmann::json t_ms = nullptr;
        if (m[3].matched) {
            try {
                const double converted =
                    to_ms(std::stod(m[3].str()), m[4].matched ? m[4].str() : "ms");
                if (std::isfinite(converted)) t_ms = converted;
            } catch (...) {
                t_ms = nullptr;
            }
        }
        events.push_back({{"type", "gpio"},
                          {"seq", static_cast<long long>(i)},
                          {"t_ms", t_ms},
                          {"pin", m[1].str()},
                          {"level", std::stoi(m[2].str())}});
    }
    return bound_events(events);
}

nlohmann::json parse_uart_events(const std::string& text) {
    nlohmann::json events = nlohmann::json::array();
    // Select transmit evidence before bounding it: unrelated bus traffic must not
    // discard mid-mission UART bytes or introduce a false UART truncation marker.
    nlohmann::json legacy_events = nlohmann::json::array();
    nlohmann::json t_ms = nullptr;
    auto lines = split_lines(text);
    for (size_t i = 0; i < lines.size(); ++i) {
        if (auto event = parse_bus_event(lines[i], events.size())) {
            const auto protocol = event->value("protocol", std::string());
            if (event->value("type", std::string()) == "trace_truncated") {
                // Sample limits and another protocol's emitter loss do not drop UART bytes.
                // An unscoped emitter loss can affect UART, so retain it conservatively.
                if (event->value("cause", std::string()) != "sample_limit" &&
                    (protocol.empty() || protocol == "uart")) {
                    events.push_back(std::move(*event));
                    legacy_events.clear();
                }
            } else if (protocol == "uart" &&
                       event->value("event", std::string()) == "tx_complete" &&
                       event->contains("t_ns")) {
                try {
                    const auto& record = *event;
                    const char byte = static_cast<char>(
                        std::stoul(record.value("value", std::string("0")), nullptr, 0));
                    events.push_back({{"type", "uart"}, {"seq", events.size()},
                                      {"t_ns", record["t_ns"]}, {"t_ms", record["t_ms"]},
                                      {"bus", record["bus"]}, {"text", std::string(1, byte)}});
                    legacy_events.clear();
                } catch (...) {}
            }
            continue;
        }
        if (!events.empty()) continue;
        std::smatch m;
        if (std::regex_search(lines[i], m, kTMarkRe)) {
            try {
                const double parsed = std::stod(m[1].str());
                if (std::isfinite(parsed)) t_ms = parsed;
            } catch (...) {
                t_ms = nullptr;
            }
            continue;
        }
        std::string s = trim(lines[i]);
        if (s.empty() || s.find("CHUDWARE_GPIO_EVT") != std::string::npos ||
            s.find("CHUDWARE_GPIO_ODR_") != std::string::npos || s.find("CHUDWARE_BUS_EVT")!=std::string::npos)
            continue;
        legacy_events.push_back({{"type", "uart"},
                          {"seq", static_cast<long long>(i)},
                          {"t_ms", t_ms},
                          {"text", s}});
    }
    return bound_events(events.empty() ? legacy_events : events);
}

nlohmann::json parse_renode_gpio_odr(const std::string& text) {
    nlohmann::json states = nlohmann::json::object();
    std::string port;
    bool in_block = false;
    long long value = 0;
    bool have_value = false;
    for (const auto& line : split_lines(text)) {
        std::smatch m;
        if (std::regex_search(line, m, kOdrBeginRe)) {
            port = m[1].str();
            in_block = true;
            have_value = false;
            continue;
        }
        if (std::regex_search(line, m, kOdrEndRe)) {
            if (in_block && port == m[1].str() && have_value) {
                for (int bit = 0; bit < 16; ++bit)
                    states["P" + port + std::to_string(bit)] = (value >> bit) & 1;
            }
            in_block = false;
            have_value = false;
            continue;
        }
        if (in_block && !have_value && std::regex_search(line, m, kHexRe)) {
            try {
                value = std::stoll(m[0].str(), nullptr, 16);
                have_value = true;
            } catch (...) {
                have_value = false;
            }
        }
    }
    return states;
}

nlohmann::json derive_gpio_states(const nlohmann::json& events,
                                  const nlohmann::json& odr_states) {
    nlohmann::json final = nlohmann::json::object();
    for (const auto& ev : events)
        if (ev.contains("pin") && ev["pin"].is_string())
            final[ev["pin"].get<std::string>()] = ev.value("level", 0);
    if (odr_states.is_object())
        for (auto& [k, v] : odr_states.items()) final[k] = v;
    return final;
}

std::vector<std::pair<char, int>> pins_from_assertions(
    const std::vector<std::string>& assertions) {
    std::vector<std::pair<char, int>> pins;
    for (auto a : assertions) {
        a = trim(a);
        std::string spec;
        if (a.rfind("gpio:", 0) == 0) {
            spec = a.substr(5);
        } else if (a.rfind("toggles:", 0) == 0) {
            spec = a.substr(8);
            static const std::regex opRe(R"(>=|<=|==|>|<)");
            std::smatch m;
            if (std::regex_search(spec, m, opRe)) spec = spec.substr(0, m.position());
        } else if (a.rfind("period:", 0) == 0) {
            spec = a.substr(7);
        } else {
            continue;
        }
        std::string pin_name = trim(spec.substr(0, spec.find('=')));
        std::smatch m;
        if (std::regex_match(pin_name, m, kStm32PinRe)) {
            char port = m[1].str()[0];
            int num = std::stoi(m[2].str());
            if (num <= 15 &&
                std::find(pins.begin(), pins.end(), std::make_pair(port, num)) == pins.end())
                pins.emplace_back(port, num);
        }
    }
    return pins;
}

std::optional<std::string> write_trace_artifact(const std::string& elf_path,
                                                const std::string& source,
                                                const std::string& uart_log,
                                                const nlohmann::json& gpio_events,
                                                const nlohmann::json& gpio_states) {
    try {
        namespace fs = std::filesystem;
        std::error_code ec;
        if (!fs::is_regular_file(elf_path, ec)) return std::nullopt;
        nlohmann::json events = nlohmann::json::array();
        for (const auto& e : gpio_events) events.push_back(e);
        for (const auto& e : parse_uart_events(uart_log)) events.push_back(e);
        std::stable_sort(events.begin(), events.end(),
                         [](const nlohmann::json& a, const nlohmann::json& b) {
                             return a.value("seq", 0LL) < b.value("seq", 0LL);
                         });
        events = bound_events(events);
        const bool truncated = events_truncated(events);
        nlohmann::json payload = {
            {"schema", "aicad.firmware.trace/1"},
            {"source", source},
            {"generated_at", std::chrono::duration<double>(
                                 std::chrono::system_clock::now().time_since_epoch())
                                 .count()},
            {"events", events},
            {"gpio_final", gpio_states},
            {"truncated", truncated},
        };
        fs::path out = elf_path;
        out += ".trace.json";
        fs::path tmp = out;
        tmp += ".tmp";
        {
            std::ofstream file(tmp, std::ios::binary | std::ios::trunc);
            if (!file) return std::nullopt;
            file << payload.dump(2);
            file.flush();
            if (!file) {
                fs::remove(tmp, ec);
                return std::nullopt;
            }
        }
        fs::rename(tmp, out, ec);
        if (ec && fs::is_regular_file(out)) {
            std::error_code remove_ec;
            fs::remove(out, remove_ec);
            if (!remove_ec) {
                ec.clear();
                fs::rename(tmp, out, ec);
            }
        }
        if (ec) {
            fs::remove(tmp, ec);
            return std::nullopt;
        }
        return out.string();
    } catch (...) {
        return std::nullopt;
    }
}

nlohmann::json evaluate_assertions(const std::string& uart_log,
                                   const nlohmann::json& gpio_states,
                                   const std::vector<std::string>& assertions,
                                   const nlohmann::json& gpio_events_in,
                                   const nlohmann::json& uart_events_in) {
    nlohmann::json results = nlohmann::json::array();
    const nlohmann::json& gpio_events = gpio_events_in.is_array() ? gpio_events_in
                                                                  : nlohmann::json::array();
    const nlohmann::json& uart_events = uart_events_in.is_array() ? uart_events_in
                                                                  : nlohmann::json::array();
    const bool gpio_trace_truncated = events_truncated(gpio_events);
    const bool uart_trace_truncated = events_truncated(uart_events);
    const bool precise_uart = std::any_of(uart_events.begin(), uart_events.end(),
        [](const auto& event) { return event.contains("t_ns"); });
    const auto transmitted = precise_uart && !uart_trace_truncated
        ? uart_streams(uart_events) : std::map<std::string, std::string>();

    for (const auto& raw_assertion : assertions) {
        std::string a = trim(raw_assertion);
        if (a.rfind("toggles:", 0) == 0) {
            if (gpio_trace_truncated) {
                results.push_back(assertion_result(
                    a, false, "not evaluated: GPIO trace exceeded the event limit"));
                continue;
            }
            std::smatch m;
            std::string spec = trim(a.substr(8));
            if (!std::regex_match(spec, m, kTogglesRe)) {
                results.push_back(assertion_result(
                    a, false, "toggles assertion must be toggles:PIN<op>N (op: >= <= == > <)"));
                continue;
            }
            std::string pin = m[1].str(), op = m[2].str();
            long n = std::stol(m[3].str());
            long count = count_toggles(gpio_events, pin);
            bool ok = (op == ">=" && count >= n) || (op == "<=" && count <= n) ||
                      (op == "==" && count == n) || (op == ">" && count > n) ||
                      (op == "<" && count < n);
            results.push_back(assertion_result(
                a, ok, pin + " toggled " + std::to_string(count) + " time(s), want " + op +
                           std::to_string(n)));
            continue;
        }
        if (a.rfind("period:", 0) == 0) {
            if (gpio_trace_truncated) {
                results.push_back(assertion_result(
                    a, false, "not evaluated: GPIO trace exceeded the event limit"));
                continue;
            }
            std::smatch m;
            std::string spec = trim(a.substr(7));
            if (!std::regex_match(spec, m, kPeriodRe)) {
                results.push_back(assertion_result(
                    a, false, "period assertion must be period:PIN=<P>ms±<TOL>ms (or +-)"));
                continue;
            }
            std::string pin = m[1].str();
            double p_ref = std::stod(m[2].str()), tol = std::stod(m[3].str());
            std::vector<double> rises;
            for (const auto& e : gpio_events) {
                if (e.value("pin", std::string()) == pin && e.value("level", 0) == 1 &&
                    e.contains("t_ms") && e["t_ms"].is_number())
                    rises.push_back(e["t_ms"].get<double>());
            }
            if (rises.size() < 2) {
                results.push_back(assertion_result(
                    a, false, "need >=2 time-stamped rising edges on '" + pin + "', got " +
                                  std::to_string(rises.size())));
                continue;
            }
            double sum = 0.0;
            for (size_t i = 1; i < rises.size(); ++i) sum += rises[i] - rises[i - 1];
            double mean = sum / (rises.size() - 1);
            bool ok = std::fabs(mean - p_ref) <= tol;
            char mean_buf[32], pref_buf[32], tol_buf[32];
            std::snprintf(mean_buf, sizeof(mean_buf), "%.3g", mean);
            std::snprintf(pref_buf, sizeof(pref_buf), "%g", p_ref);
            std::snprintf(tol_buf, sizeof(tol_buf), "%g", tol);
            results.push_back(assertion_result(
                a, ok, pin + " mean period " + mean_buf + "ms over " +
                           std::to_string(rises.size() - 1) + " interval(s), want " + pref_buf +
                           "±" + tol_buf + "ms"));
            continue;
        }
        if (a.rfind("uart_within:", 0) == 0) {
            std::smatch m;
            std::string spec = trim(a.substr(12));
            if (!std::regex_match(spec, m, kUartWithinRe)) {
                results.push_back(assertion_result(
                    a, false, "uart_within assertion must be uart_within:<text>@<T>ms"));
                continue;
            }
            if (uart_trace_truncated) {
                results.push_back(assertion_result(a,false,"not evaluated: UART/bus evidence exceeded event limit"));continue;
            }
            std::string text = m[1].str();
            double t_ref = std::stod(m[2].str());
            const bool stamped=std::any_of(uart_events.begin(),uart_events.end(),[](const auto& event) {
                return event.contains("t_ms") && event["t_ms"].is_number();
            });
            if (!stamped) {
                results.push_back(assertion_result(
                    a, false, "no time-stamped UART events (timed STM32F4 events require real model transmit completion)"));
                continue;
            }
            const nlohmann::json* hit = nullptr;
            const nlohmann::json* later = nullptr;
            std::map<std::string,std::string> streams;
            for (const auto& e : uart_events) {
                if(!e.contains("t_ms") || !e["t_ms"].is_number()) continue;
                auto& stream=streams[e.value("bus",std::string())];
                const std::string fragment=e.value("text",std::string());
                const size_t old_size=stream.size();
                stream+=fragment;
                const size_t from=old_size>=text.size()?old_size-text.size()+1:0;
                if(stream.find(text,from)!=std::string::npos) {
                    if(e["t_ms"].get<double>()<=t_ref) {hit=&e;break;}
                    if(!later) later=&e;
                }
            }
            if (hit) {
                results.push_back(assertion_result(
                    a, true, "found at t<=" + fmt_g((*hit)["t_ms"].get<double>()) + "ms"));
            } else if (later) {
                results.push_back(assertion_result(
                    a, false, "first match at t=" + fmt_g((*later)["t_ms"].get<double>()) +
                                  "ms > " + fmt_g(t_ref) + "ms"));
            } else {
                results.push_back(assertion_result(
                    a, false, "'" + text + "' not found in time-stamped uart events"));
            }
            continue;
        }
        if (a.rfind("gpio:", 0) == 0) {
            std::string spec = trim(a.substr(5));
            std::smatch tm;
            if (std::regex_match(spec, tm, kGpioTimeRe)) {
                std::string pin = trim(tm[1].str());
                std::string op = tm[3].str();
                double t_ref = std::stod(tm[4].str());
                int expected;
                try {
                    expected = std::stoi(trim(tm[2].str()));
                } catch (...) {
                    results.push_back(assertion_result(
                        a, false, "invalid level '" + tm[2].str() + "'"));
                    continue;
                }
                std::vector<nlohmann::json> stamped;
                for (const auto& e : gpio_events) {
                    if (e.value("pin", std::string()) == pin && e.contains("t_ms") &&
                        e["t_ms"].is_number())
                        stamped.push_back(e);
                }
                if (stamped.empty()) {
                    results.push_back(assertion_result(
                        a, false, "no time-stamped events for pin '" + pin + "'"));
                    continue;
                }
                const nlohmann::json* hit = nullptr;
                for (const auto& e : stamped) {
                    double t = e["t_ms"].get<double>();
                    int lvl = e.value("level", 0);
                    bool match = (op == ">" && t > t_ref && lvl == expected) ||
                                 (op == "<" && t < t_ref && lvl == expected) ||
                                 (op.empty() && t >= t_ref && lvl == expected);
                    if (match) {
                        hit = &e;
                        break;
                    }
                }
                if (hit) {
                    results.push_back(assertion_result(
                        a, true, pin + "=" + std::to_string(expected) + " at t=" +
                                     fmt_g((*hit)["t_ms"].get<double>()) + "ms"));
                } else {
                    std::string seen;
                    for (size_t i = 0; i < stamped.size() && i < 8; ++i) {
                        if (i) seen += ", ";
                        seen += std::to_string(stamped[i].value("level", 0)) + "@" +
                                fmt_g(stamped[i]["t_ms"].get<double>()) + "ms";
                    }
                    results.push_back(assertion_result(
                        a, false, "no event " + pin + "=" + std::to_string(expected) + " " +
                                      (op.empty() ? ">=" : op) + fmt_g(t_ref) +
                                      "ms (events: " + seen + ")"));
                }
                continue;
            }
            if (spec.find('=') != std::string::npos) {
                std::string pin = trim(spec.substr(0, spec.find('=')));
                std::string lvl_str = trim(spec.substr(spec.find('=') + 1));
                try {
                    int expected = std::stoi(lvl_str);
                    if (!gpio_states.is_object() || !gpio_states.contains(pin)) {
                        results.push_back(assertion_result(
                            a, false, "pin '" + pin + "' not in gpio_states"));
                    } else {
                        int actual = gpio_states[pin].get<int>();
                        if (actual == expected)
                            results.push_back(assertion_result(
                                a, true, pin + "=" + std::to_string(actual)));
                        else
                            results.push_back(assertion_result(
                                a, false, pin + "=" + std::to_string(actual) + " != " +
                                              std::to_string(expected)));
                    }
                } catch (...) {
                    results.push_back(assertion_result(a, false,
                                                       "invalid level '" + lvl_str + "'"));
                }
            } else {
                results.push_back(
                    assertion_result(a, false, "gpio assertion must be gpio:PIN=LEVEL"));
            }
        } else {
            std::string text = a.rfind("uart:", 0) == 0 ? a.substr(5) : a;
            if (uart_trace_truncated) {
                results.push_back(assertion_result(
                    a, false, "not evaluated: UART/bus evidence exceeded event limit"));
            } else {
                const bool found = precise_uart
                    ? std::any_of(transmitted.begin(), transmitted.end(), [&](const auto& stream) {
                          return stream.second.find(text) != std::string::npos;
                      })
                    : uart_log.find(text) != std::string::npos;
                const std::string source = precise_uart ? "transmitted UART data" : "uart_log";
                results.push_back(assertion_result(
                    a, found, found ? "found in " + source : "'" + text + "' not found in " + source));
            }
        }
    }
    return results;
}

std::string build_resc(const std::string& elf_path, const std::string& platform_file,
                       double run_seconds,
                       const std::vector<std::pair<char, int>>& gpio_pins_in,
                       const nlohmann::json& gpio_regmap,
                       const std::vector<std::string>& scenario_setup_lines,
                       const std::vector<std::pair<double, std::string>>& scenario_timeline) {
    std::string platform_text;
    {
        std::ifstream in(platform_file);
        std::stringstream ss;
        ss << in.rdbuf();
        platform_text = ss.str();
    }
    std::string uart_name;
    {
        std::istringstream in(platform_text);
        std::string ln;
        while (std::getline(in, ln)) {
            std::smatch m;
            if (std::regex_search(ln, m, kReplUartRe)) {
                uart_name = m[1].str();
                break;
            }
        }
    }

    // Only retain pins whose final state can be read from a declared GPIO port.
    std::vector<std::pair<char, int>> gpio_pins;
    for (const auto& [p, n] : gpio_pins_in) {
        if (platform_text.find(std::string("gpioPort") + p) != std::string::npos)
            gpio_pins.emplace_back(p, n);
    }

    long long base = 0x40020000, stride = 0x400, odr_offset = 0x14;
    if (gpio_regmap.is_object()) {
        base = gpio_regmap.value("base", base);
        stride = gpio_regmap.value("stride", stride);
        odr_offset = gpio_regmap.value("odr_offset", odr_offset);
    }

    std::vector<std::string> lines = {"mach create"};
    for(const auto& l:scenario_setup_lines) if(l.rfind("include ",0)==0) lines.push_back(l);
    lines.push_back("machine LoadPlatformDescription " + renode_path(platform_file));
    for(const auto& l:scenario_setup_lines) if(l.rfind("include ",0)!=0) lines.push_back(l);
    lines.push_back("sysbus LoadELF " + renode_path(elf_path));
    if (!uart_name.empty()) lines.push_back("showAnalyzer sysbus." + uart_name);
    for (const auto& l : timeline_resc_lines(scenario_timeline, run_seconds))
        lines.push_back(l);
    std::set<char> ports;
    for (const auto& [p, _] : gpio_pins) ports.insert(p);
    for (char port : ports) {
        long long odr = base + stride * (port - 'A') + odr_offset;
        lines.push_back("echo " + renode_string("CHUDWARE_GPIO_ODR_BEGIN " + std::string(1, port)));
        lines.push_back("sysbus ReadDoubleWord " + fmt_hex(odr));
        lines.push_back("echo " + renode_string("CHUDWARE_GPIO_ODR_END " + std::string(1, port)));
    }
    lines.push_back("quit");
    std::string out;
    for (const auto& l : lines) {
        out += l;
        out += '\n';
    }
    return out;
}

SimResult run_simulation(const std::string& elf_path, const std::string& mcu_family,
                         const std::vector<std::string>& assertions, double timeout_s,
                         const nlohmann::json& scenario, double run_s, const std::string& backend) {
    auto t0 = std::chrono::steady_clock::now();
    try {
    if (!std::isfinite(timeout_s) || timeout_s <= 0.0)
        return refused_result(
            "invalid_setup: timeout_s must be finite and greater than zero", assertions, t0);
    if(!std::isfinite(run_s) || run_s<0 || (backend!="auto"&&backend!="renode"&&backend!="qemu"&&backend!="simavr"))
        return refused_result("invalid_setup: run_s must be finite nonnegative and backend auto/renode/qemu/simavr",assertions,t0);
    if(backend=="simavr") {
        if(!scenario.is_null()) return refused_result("scenario_unsupported: simavr has no timed bus scenario surface",assertions,t0);
        const auto invalid=image_error(elf_path,83);
        if(!invalid.empty()) return refused_result(invalid,assertions,t0);
        auto legacy=simavr_run(elf_path,mcu_family,run_s>0?run_s:1.0,1234,{},timeout_s);
        SimResult result;result.uart_log=legacy.output;result.ms=legacy.ms;
        result.assertion_results=evaluate_assertions(result.uart_log,result.gpio_states,assertions,result.gpio_events,parse_uart_events(result.uart_log));
        result.passed=legacy.success;
        for(const auto& assertion:result.assertion_results) if(!assertion.value("passed",false)) result.passed=false;
        result.bus_summary={{"limitations",{"simavr run_s is legacy wall-duration stop, not virtual timed/electrical bus evidence"}}};
        return result;
    }
    const auto setup_errors = assertion_errors(assertions);
    if (!setup_errors.empty()) {
        std::string joined;
        for (const auto& error : setup_errors) {
            if (!joined.empty()) joined += "; ";
            joined += error;
        }
        return refused_result("invalid_setup: " + joined, assertions, t0);
    }
    if (!scenario.is_null() && !scenario.is_object())
        return refused_result(
            "invalid_setup: scenario must be a validated aicad.fwscenario/1 object",
            assertions, t0);
    nlohmann::json caps = get_capabilities(mcu_family);

    if (caps.value("emulation", std::string()) == "none") {
        std::set<std::string> all(assertions.begin(), assertions.end());
        return unsupported_result(caps, assertions, all,
                                  "no emulation path for this family (build + flash only)", t0);
    }
    const std::string invalid_image = image_error(elf_path, 40);  // EM_ARM
    if (!invalid_image.empty()) return refused_result(invalid_image, assertions, t0);

    std::string platform = get_platform_file(mcu_family);
    bool have_renode = backend!="qemu" && !which("renode").empty();
    bool have_qemu = backend!="renode" && !which("qemu-system-arm").empty();
    if(run_s>0 && !have_renode && have_qemu)
        return refused_result("unsupported_timing: QEMU backend has no precise virtual RunFor stop surface; use Renode for run_s",assertions,t0);

    if (have_renode && !platform.empty()) {
        std::set<std::string> untimestampable;
        for (const auto& assertion : assertions)
            if (requires_timestamped_gpio_observation(assertion))
                untimestampable.insert(assertion);
        if (!untimestampable.empty()) {
            return unsupported_result(
                caps, assertions, untimestampable,
                "timestamped GPIO observation is unavailable on the supported Renode "
                "1.16.1 command surface; AddStateChangedHook is unsupported and the final "
                "ODR snapshot carries state but no emulator time",
                t0);
        }
        std::set<std::string> unsupported;
        for (const auto& a : assertions) {
            std::string cap_needed = assertion_capability(a);
            if ((cap_needed == "gpio" && !caps.value("gpio_observe", false)) ||
                (cap_needed == "uart" && !caps.value("uart", false)))
                unsupported.insert(a);
        }
        if (!unsupported.empty() && scenario.is_object()) {
            return unsupported_result(
                caps, assertions, unsupported,
                "platform cannot observe what the assertions need, and the scenario "
                "requires the Renode path (QEMU has no peripheral stimulus)",
                t0);
        }
        if (!unsupported.empty()) {
            if (have_qemu) return run_qemu(elf_path, timeout_s, assertions);
            std::set<std::string> missing;
            for (const auto& a : unsupported)
                missing.insert(assertion_capability(a) == "gpio" ? "gpio observation"
                                                                 : "a UART model");
            std::string joined;
            for (const auto& m : missing) {
                if (!joined.empty()) joined += " and ";
                joined += m;
            }
            return unsupported_result(
                caps, assertions, unsupported,
                "platform lacks " + joined +
                    " required by the assertions and no QEMU UART fallback is installed",
                t0);
        }
        return run_renode(elf_path, platform, assertions, timeout_s, caps["gpio_regmap"],
                          scenario, caps, run_s);
    }
    if (have_qemu) {
        if (scenario.is_object()) {
            SimResult r;
            r.passed = false;
            r.uart_log =
                "scenario_unsupported: stimulus scenarios need the Renode path (QEMU exposes "
                "no peripheral stimulus surface). Install Renode (https://renode.io) to run "
                "this scenario.";
            for (const auto& a : assertions)
                r.assertion_results.push_back(assertion_result(
                    a, false, "not evaluated: scenario requires Renode"));
            r.ms = std::chrono::duration<double, std::milli>(
                       std::chrono::steady_clock::now() - t0)
                       .count();
            return r;
        }
        return run_qemu(elf_path, timeout_s, assertions);
    }
    return refused_result(
        "emulator_not_found: neither 'renode' nor 'qemu-system-arm' found on PATH. Install "
        "Renode (https://renode.io) or QEMU for live emulation.",
        assertions, t0);
    } catch (const std::exception& error) {
        return refused_result("invalid_setup: " + std::string(error.what()), assertions, t0);
    }
}

}  // namespace chudware::firmware
