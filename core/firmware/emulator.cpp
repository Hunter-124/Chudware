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

namespace aicad::firmware {
namespace {

constexpr size_t kUartCap = 32000;

// AICAD_GPIO_EVT <PIN>=<level>[ t=<num>[ms|us|s]] — emitted by BOTH backends.
const std::regex kGpioEvtRe(
    R"(AICAD_GPIO_EVT\s+([A-Za-z_][A-Za-z0-9_]*)=([01])(?:\s+t=([0-9]+(?:\.[0-9]+)?)(ms|us|s)?)?)");
const std::regex kOdrBeginRe(R"(AICAD_GPIO_ODR_BEGIN\s+([A-K]))");
const std::regex kOdrEndRe(R"(AICAD_GPIO_ODR_END\s+([A-K]))");
const std::regex kHexRe(R"(0x[0-9a-fA-F]+)");
const std::regex kTMarkRe(R"(AICAD_T_MARK\s+([0-9]+(?:\.[0-9]+)?))");
const std::regex kStm32PinRe(R"(^P([A-K])(\d{1,2})$)");
const std::regex kReplUartRe(R"(^(\w+)\s*:\s*UART\.)");

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

std::string trim(const std::string& s) {
    size_t b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos) return "";
    return s.substr(b, s.find_last_not_of(" \t\r\n") - b + 1);
}

SimResult emulator_not_found(const std::string& binary,
                             std::chrono::steady_clock::time_point t0) {
    SimResult r;
    r.passed = false;
    r.uart_log = "emulator_not_found: '" + binary + "' is not installed / not on PATH";
    r.ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    return r;
}

nlohmann::json assertion_result(const std::string& name, bool passed,
                                const std::string& detail) {
    return {{"name", name}, {"passed", passed}, {"detail", detail}};
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

// Renode backend (emulator.py::RenodeEmulator.run).
SimResult run_renode(const std::string& elf_path, const std::string& platform_file,
                     const std::vector<std::string>& assertions, double timeout_s,
                     const nlohmann::json& gpio_regmap, const nlohmann::json& scenario,
                     const nlohmann::json& mcu_caps) {
    auto t0 = std::chrono::steady_clock::now();
    if (which("renode").empty()) return emulator_not_found("renode", t0);

    double run_seconds = std::max(1.0, timeout_s - 5.0);
    auto gpio_pins = pins_from_assertions(assertions);

    // Compile the scenario (T-FIN3) — pure plan first, then aux files on disk.
    std::vector<std::string> scenario_setup;
    std::vector<std::pair<double, std::string>> scenario_timeline;
    std::string scenario_dir;
    if (scenario.is_object()) {
        nlohmann::json caps = mcu_caps.is_object() ? mcu_caps : get_capabilities("cortex-m");
        std::string platform_text;
        {
            std::ifstream in(platform_file);
            std::stringstream ss;
            ss << in.rdbuf();
            platform_text = ss.str();
        }
        ScenarioPlan plan = build_scenario_plan(scenario, caps, platform_text);
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
        namespace fs = std::filesystem;
        fs::path dir = fs::temp_directory_path() /
                       fs::path("aicad_scenario_" + std::to_string(
                           std::chrono::steady_clock::now().time_since_epoch().count()));
        scenario_dir = dir.string();
        materialize_aux_files(plan, scenario_dir);
        scenario_setup = plan.setup_lines;
        scenario_timeline = plan.timeline;
        run_seconds = std::max(run_seconds, plan.duration_ms / 1000.0 + 1.0);
        timeout_s = std::max(timeout_s, run_seconds + 5.0);
    }

    std::string resc = build_resc(elf_path, platform_file, run_seconds, gpio_pins, gpio_regmap,
                                  scenario_setup, scenario_timeline);
    namespace fs = std::filesystem;
    fs::path resc_path = fs::temp_directory_path() /
                         ("aicad_" + std::to_string(
                              std::chrono::steady_clock::now().time_since_epoch().count()) +
                          ".resc");
    {
        std::ofstream out(resc_path, std::ios::binary | std::ios::trunc);
        out << resc;
    }

    std::vector<std::string> cmd = {"renode", "--disable-xwt", "--console", resc_path.string()};
    RunResult rr = run_capture(cmd, "", timeout_s);
    std::error_code ec;
    fs::remove(resc_path, ec);
    if (!scenario_dir.empty()) fs::remove_all(scenario_dir, ec);

    if (rr.not_found) return emulator_not_found("renode", t0);
    if (rr.timed_out) {
        SimResult r;
        r.passed = false;
        r.uart_log = "timeout: renode exceeded " +
                     std::to_string(static_cast<long long>(timeout_s)) + "s";
        r.ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
                   .count();
        return r;
    }
    std::string raw = rr.stdout_text;
    if (rr.returncode != 0 && rr.returncode != 1) raw += rr.stderr_text;

    std::string uart_log = cap(raw);
    nlohmann::json gpio_events = parse_gpio_events(raw);
    nlohmann::json gpio_states = derive_gpio_states(gpio_events, parse_renode_gpio_odr(raw));
    nlohmann::json uart_events = parse_uart_events(raw);
    auto trace_path = write_trace_artifact(elf_path, "renode", raw, gpio_events, gpio_states);
    SimResult r;
    r.assertion_results =
        evaluate_assertions(uart_log, gpio_states, assertions, gpio_events, uart_events);
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
    if (which("qemu-system-arm").empty()) return emulator_not_found("qemu-system-arm", t0);
    std::vector<std::string> cmd = {
        "qemu-system-arm", "-M", "mps2-an385", "-cpu", "cortex-m3",
        "-kernel", elf_path, "-semihosting",
        "-semihosting-config", "enable=on,target=native", "-nographic", "-no-reboot",
    };
    RunResult rr = run_capture(cmd, "", timeout_s);
    if (rr.not_found) return emulator_not_found("qemu-system-arm", t0);
    if (rr.timed_out) {
        SimResult r;
        r.passed = false;
        r.uart_log = "timeout: qemu-system-arm exceeded " +
                     std::to_string(static_cast<long long>(timeout_s)) + "s";
        r.ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
                   .count();
        return r;
    }
    std::string raw = rr.stdout_text;
    std::string uart_log = cap(raw);
    nlohmann::json gpio_events = parse_gpio_events(raw);
    nlohmann::json gpio_states = derive_gpio_states(gpio_events, nlohmann::json::object());
    auto trace_path = write_trace_artifact(elf_path, "qemu", raw, gpio_events, gpio_states);
    SimResult r;
    r.assertion_results = evaluate_assertions(uart_log, gpio_states, assertions, gpio_events,
                                              parse_uart_events(raw));
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
        {"trace_path", trace_path ? nlohmann::json(*trace_path) : nlohmann::json(nullptr)},
    };
}

nlohmann::json parse_gpio_events(const std::string& text) {
    nlohmann::json events = nlohmann::json::array();
    auto lines = split_lines(text);
    for (size_t i = 0; i < lines.size(); ++i) {
        std::smatch m;
        if (!std::regex_search(lines[i], m, kGpioEvtRe)) continue;
        nlohmann::json t_ms = nullptr;
        if (m[3].matched) {
            double t = std::stod(m[3].str());
            t_ms = to_ms(t, m[4].matched ? m[4].str() : "ms");
        }
        events.push_back({{"type", "gpio"},
                          {"seq", static_cast<long long>(i)},
                          {"t_ms", t_ms},
                          {"pin", m[1].str()},
                          {"level", std::stoi(m[2].str())}});
    }
    return events;
}

nlohmann::json parse_uart_events(const std::string& text) {
    nlohmann::json events = nlohmann::json::array();
    nlohmann::json t_ms = nullptr;
    auto lines = split_lines(text);
    for (size_t i = 0; i < lines.size(); ++i) {
        std::smatch m;
        if (std::regex_search(lines[i], m, kTMarkRe)) {
            t_ms = std::stod(m[1].str());
            continue;
        }
        std::string s = trim(lines[i]);
        if (s.empty() || s.find("AICAD_GPIO_EVT") != std::string::npos ||
            s.find("AICAD_GPIO_ODR_") != std::string::npos)
            continue;
        events.push_back({{"type", "uart"},
                          {"seq", static_cast<long long>(i)},
                          {"t_ms", t_ms},
                          {"text", s}});
    }
    return events;
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
            value = std::stoll(m[0].str(), nullptr, 16);
            have_value = true;
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
        if (!fs::exists(elf_path, ec)) return std::nullopt;
        nlohmann::json events = nlohmann::json::array();
        for (const auto& e : gpio_events) events.push_back(e);
        for (const auto& e : parse_uart_events(uart_log)) events.push_back(e);
        std::stable_sort(events.begin(), events.end(),
                         [](const nlohmann::json& a, const nlohmann::json& b) {
                             return a.value("seq", 0LL) < b.value("seq", 0LL);
                         });
        nlohmann::json payload = {
            {"schema", "aicad.firmware.trace/1"},
            {"source", source},
            {"generated_at", std::chrono::duration<double>(
                                 std::chrono::system_clock::now().time_since_epoch())
                                 .count()},
            {"events", events},
            {"gpio_final", gpio_states},
        };
        fs::path out = elf_path;
        out += ".trace.json";
        fs::path tmp = out;
        tmp += ".tmp";
        {
            std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
            f << payload.dump(2);
        }
        fs::rename(tmp, out, ec);
        if (ec) return std::nullopt;
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

    for (const auto& raw_assertion : assertions) {
        std::string a = trim(raw_assertion);
        if (a.rfind("toggles:", 0) == 0) {
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
            std::string text = m[1].str();
            double t_ref = std::stod(m[2].str());
            std::vector<nlohmann::json> stamped;
            for (const auto& e : uart_events)
                if (e.contains("t_ms") && e["t_ms"].is_number()) stamped.push_back(e);
            if (stamped.empty()) {
                results.push_back(assertion_result(
                    a, false, "no time-stamped uart events (uart timestamps come from the "
                              "scenario timeline's AICAD_T_MARK markers)"));
                continue;
            }
            const nlohmann::json* hit = nullptr;
            const nlohmann::json* later = nullptr;
            for (const auto& e : stamped) {
                if (e.value("text", std::string()).find(text) != std::string::npos) {
                    if (e["t_ms"].get<double>() <= t_ref) {
                        hit = &e;
                        break;
                    }
                    if (!later) later = &e;
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
            if (uart_log.find(text) != std::string::npos)
                results.push_back(assertion_result(a, true, "found in uart_log"));
            else
                results.push_back(
                    assertion_result(a, false, "'" + text + "' not found in uart_log"));
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

    // Only wire hooks/reads for ports the platform actually declares.
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

    std::vector<std::string> lines = {"mach create",
                                      "machine LoadPlatformDescription @" + platform_file};
    for (const auto& l : scenario_setup_lines) lines.push_back(l);
    lines.push_back("sysbus LoadELF @" + elf_path);
    if (!uart_name.empty()) lines.push_back("showAnalyzer sysbus." + uart_name);
    for (const auto& [port, num] : gpio_pins) {
        lines.push_back("sysbus.gpioPort" + std::string(1, port) + " AddStateChangedHook " +
                        std::to_string(num) + " \"print('AICAD_GPIO_EVT P" + port +
                        std::to_string(num) + "=%d' % (1 if state else 0))\"");
    }
    lines.push_back("start");
    if (!scenario_timeline.empty()) {
        for (const auto& l : timeline_resc_lines(scenario_timeline, run_seconds))
            lines.push_back(l);
    } else {
        char buf[32];
        std::snprintf(buf, sizeof(buf), "%.1f", run_seconds);
        lines.push_back(std::string("sleep ") + buf);
    }
    std::set<char> ports;
    for (const auto& [p, _] : gpio_pins) ports.insert(p);
    for (char port : ports) {
        long long odr = base + stride * (port - 'A') + odr_offset;
        lines.push_back("echo \"AICAD_GPIO_ODR_BEGIN " + std::string(1, port) + "\"");
        lines.push_back("sysbus ReadDoubleWord " + fmt_hex(odr));
        lines.push_back("echo \"AICAD_GPIO_ODR_END " + std::string(1, port) + "\"");
    }
    std::string out;
    for (const auto& l : lines) {
        out += l;
        out += '\n';
    }
    return out;
}

SimResult run_simulation(const std::string& elf_path, const std::string& mcu_family,
                         const std::vector<std::string>& assertions, double timeout_s,
                         const nlohmann::json& scenario) {
    auto t0 = std::chrono::steady_clock::now();
    nlohmann::json caps = get_capabilities(mcu_family);

    if (caps.value("emulation", std::string()) == "none") {
        std::set<std::string> all(assertions.begin(), assertions.end());
        return unsupported_result(caps, assertions, all,
                                  "no emulation path for this family (build + flash only)", t0);
    }

    std::string platform = get_platform_file(mcu_family);
    bool have_renode = !which("renode").empty();
    bool have_qemu = !which("qemu-system-arm").empty();

    if (have_renode && !platform.empty()) {
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
                    " required by the assertions and no QEMU semihosting fallback is installed",
                t0);
        }
        return run_renode(elf_path, platform, assertions, timeout_s, caps["gpio_regmap"],
                          scenario, caps);
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
    SimResult r;
    r.passed = false;
    r.uart_log =
        "emulator_not_found: neither 'renode' nor 'qemu-system-arm' found on PATH. Install "
        "Renode (https://renode.io) or QEMU for live emulation. Tests that mock the subprocess "
        "run without either.";
    r.ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
               .count();
    return r;
}

}  // namespace aicad::firmware
