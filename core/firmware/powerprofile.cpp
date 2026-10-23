#include "firmware/powerprofile.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <map>
#include <regex>
#include <set>
#include <vector>

#include "firmware/eng_rails.h"
#include "firmware/mcu_pinouts.h"
#include "firmware/pinmap.h"
#include "firmware/pyfmt.h"

namespace chudware::firmware {
namespace {

// Firmware-side CPU state marker: CHUDWARE_CPU_EVT run t=120ms | sleep t=120000us.
const std::regex kCpuEvtRe(
    R"(CHUDWARE_CPU_EVT\s+(run|sleep)(?:\s+t=([0-9]+(?:\.[0-9]+)?)(ms|us|s)?)?)");

const std::set<std::string> kGroundNames = {"GND", "GNDA", "GNDD", "AGND", "DGND", "PGND",
                                            "EGND", "VSS", "VSSA", "EARTH", "CHASSIS"};

double round_n(double v, int digits) {
    // Python round() is round-half-to-EVEN (banker's), std::round half-away.
    double p = std::pow(10.0, digits);
    double x = v * p;
    double fl = std::floor(x);
    double diff = x - fl;
    double r;
    if (diff < 0.5) r = fl;
    else if (diff > 0.5) r = fl + 1.0;
    else r = std::fmod(fl, 2.0) == 0.0 ? fl : fl + 1.0;
    return r / p;
}

double to_ms(double t, const std::string& unit) {
    if (unit == "s") return t * 1000.0;
    if (unit == "us") return t / 1000.0;
    return t;
}

// split_events: (gpio_events, uart_events) from a trace artifact dict OR a
// SimResult-shaped dict. Seq order preserved.
std::pair<nlohmann::json, nlohmann::json> split_events(const nlohmann::json& trace) {
    nlohmann::json gpio = nlohmann::json::array(), uart = nlohmann::json::array();
    if (trace.contains("events") && trace["events"].is_array()) {
        for (const auto& e : trace["events"]) {
            if (!e.is_object()) continue;
            std::string type = e.value("type", std::string());
            if (type == "gpio") gpio.push_back(e);
            else if (type == "uart") uart.push_back(e);
        }
        return {gpio, uart};
    }
    if (trace.contains("gpio_events") && trace["gpio_events"].is_array()) {
        for (const auto& e : trace["gpio_events"])
            if (e.is_object()) gpio.push_back(e);
    }
    return {gpio, uart};
}

// The observable run window: explicit value wins, else max stamped t_ms, else null.
nlohmann::json run_window_ms(const nlohmann::json& gpio_events,
                             const nlohmann::json& uart_events, double explicit_ms) {
    if (explicit_ms > 0) return explicit_ms;
    double best = -1.0;
    auto scan = [&best](const nlohmann::json& events) {
        for (const auto& e : events)
            if (e.contains("t_ms") && e["t_ms"].is_number())
                best = std::max(best, e["t_ms"].get<double>());
    };
    scan(gpio_events);
    scan(uart_events);
    return best >= 0 ? nlohmann::json(best) : nlohmann::json(nullptr);
}

// (t_ms|null, state) markers from UART event text.
std::vector<std::pair<nlohmann::json, std::string>> cpu_markers(
    const nlohmann::json& uart_events) {
    std::vector<std::pair<nlohmann::json, std::string>> out;
    for (const auto& e : uart_events) {
        std::string text = e.value("text", std::string());
        std::smatch m;
        if (!std::regex_search(text, m, kCpuEvtRe)) continue;
        nlohmann::json t = nullptr;
        if (m[2].matched) {
            t = to_ms(std::stod(m[2].str()), m[3].matched ? m[3].str() : "ms");
        } else if (e.contains("t_ms") && e["t_ms"].is_number()) {
            t = e["t_ms"];
        }
        out.emplace_back(t, m[1].str());
    }
    return out;
}

nlohmann::json cpu_residency(const nlohmann::json& uart_events,
                             const nlohmann::json& window_ms) {
    std::vector<std::pair<double, std::string>> marks;
    for (const auto& [t, s] : cpu_markers(uart_events))
        if (t.is_number()) marks.emplace_back(t.get<double>(), s);
    double window = window_ms.is_number() ? window_ms.get<double>() : 0.0;
    if (!marks.empty() && window > 0) {
        std::sort(marks.begin(), marks.end(),
                  [](const auto& a, const auto& b) { return a.first < b.first; });
        double active = 0.0, now = 0.0;
        std::string state = "run";
        for (const auto& [t, s] : marks) {
            double tc = std::min(std::max(t, 0.0), window);
            if (state == "run") active += tc - now;
            now = tc;
            state = s;
        }
        if (state == "run") active += window - now;
        double frac = std::min(std::max(active / window, 0.0), 1.0);
        return {
            {"active_frac", round_n(frac, 6)},
            {"sleep_frac", round_n(1.0 - frac, 6)},
            {"active_s", round_n(frac * window / 1000.0, 6)},
            {"sleep_s", round_n((1.0 - frac) * window / 1000.0, 6)},
            {"source", "cpu_markers"},
            {"note", std::to_string(marks.size()) +
                         " CHUDWARE_CPU_EVT marker(s) integrated over " + fmt_g(window) +
                         "ms window"},
        };
    }
    std::string note =
        "no time-stamped CHUDWARE_CPU_EVT markers in trace — assuming CPU always active "
        "(conservative). Instrument sleep entry/exit with CHUDWARE_CPU_TRACE in chudware_trace.h "
        "for measured residency.";
    bool have_window = window_ms.is_number() && window > 0;
    return {
        {"active_frac", 1.0},
        {"sleep_frac", 0.0},
        {"active_s", have_window ? nlohmann::json(round_n(window / 1000.0, 6))
                                 : nlohmann::json(nullptr)},
        {"sleep_s", have_window ? nlohmann::json(0.0) : nlohmann::json(nullptr)},
        {"source", "assumed_active"},
        {"note", note},
    };
}

// Duty-cycle record for one pin: {duty, period_s, toggles, source, note?}.
nlohmann::json pin_duty(const nlohmann::json& gpio_events, const std::string& pin,
                        const nlohmann::json& window_ms, const nlohmann::json& gpio_final) {
    std::vector<nlohmann::json> evs;
    for (const auto& e : gpio_events)
        if (e.value("pin", std::string()) == pin) evs.push_back(e);
    int toggles = 0;
    for (size_t i = 1; i < evs.size(); ++i)
        if (evs[i].value("level", 0) != evs[i - 1].value("level", 0)) ++toggles;
    std::vector<nlohmann::json> stamped;
    for (const auto& e : evs)
        if (e.contains("t_ms") && e["t_ms"].is_number()) stamped.push_back(e);

    nlohmann::json period_s = nullptr;
    std::vector<double> rises;
    for (const auto& e : stamped)
        if (e.value("level", 0) == 1) rises.push_back(e["t_ms"].get<double>());
    if (rises.size() >= 2) {
        double sum = 0.0;
        for (size_t i = 1; i < rises.size(); ++i) sum += rises[i] - rises[i - 1];
        period_s = round_n((sum / (rises.size() - 1)) / 1000.0, 9);
    }

    double window = window_ms.is_number() ? window_ms.get<double>() : 0.0;
    if (!stamped.empty() && window > 0) {
        // Integrate high-time; level before the first stamped event assumed 0.
        std::sort(stamped.begin(), stamped.end(), [](const nlohmann::json& a,
                                                     const nlohmann::json& b) {
            return a["t_ms"].get<double>() < b["t_ms"].get<double>();
        });
        double high = 0.0, now = 0.0;
        int level = 0;
        for (const auto& e : stamped) {
            double t = std::min(std::max(e["t_ms"].get<double>(), 0.0), window);
            if (level == 1) high += t - now;
            now = t;
            level = e.value("level", 0);
        }
        if (level == 1) high += window - now;
        double duty = std::min(std::max(high / window, 0.0), 1.0);
        return {{"duty", round_n(duty, 6)},
                {"period_s", period_s},
                {"toggles", toggles},
                {"source", "stamped_events"}};
    }
    if (!evs.empty()) {
        return {{"duty", nullptr},
                {"period_s", period_s},
                {"toggles", toggles},
                {"source", "unstamped_events"},
                {"note", std::to_string(evs.size()) +
                             " event(s) without timestamps (Renode hook prints carry no t) — "
                             "event order known, time weighting unknown"}};
    }
    if (gpio_final.is_object() && gpio_final.contains(pin)) {
        double lvl = gpio_final[pin].is_number() ? gpio_final[pin].get<double>() : 0.0;
        return {{"duty", lvl},
                {"period_s", nullptr},
                {"toggles", 0},
                {"source", "final_state_only"},
                {"note", "no events observed — duty taken from the end-of-run snapshot"}};
    }
    return {{"duty", nullptr}, {"period_s", nullptr}, {"toggles", 0}, {"source", "no_data"}};
}

nlohmann::json gpio_duty_cycles(const nlohmann::json& gpio_events,
                                const nlohmann::json& gpio_final,
                                const nlohmann::json& window_ms) {
    std::set<std::string> pins;
    for (const auto& e : gpio_events)
        if (e.contains("pin") && e["pin"].is_string() && !e["pin"].get<std::string>().empty())
            pins.insert(e["pin"].get<std::string>());
    if (gpio_final.is_object())
        for (auto& [k, _] : gpio_final.items()) pins.insert(k);
    nlohmann::json out = nlohmann::json::object();
    for (const auto& p : pins) out[p] = pin_duty(gpio_events, p, window_ms, gpio_final);
    return out;
}

// {physical pin_id: net name} for positional nets (pinmap.pin_model).
std::map<long long, std::string> pin_net_map(const nlohmann::json& board,
                                             const std::string& mcu_family) {
    std::map<long long, std::string> out;
    for (const auto& rec : pin_model(board, mcu_family)) {
        if (rec.value("positional", false) && rec["pin_id"].is_number_integer())
            out[rec["pin_id"].get<long long>()] = rec["net"].get<std::string>();
    }
    return out;
}

// (active_frac|null, source) for one component.
std::pair<nlohmann::json, std::string> component_activity(const nlohmann::json& fp, bool is_mcu,
                                                          const nlohmann::json& cpu,
                                                          const nlohmann::json& net_duty) {
    if (is_mcu) return {cpu["active_frac"], "cpu_residency:" + cpu.value("source", std::string())};
    if (fp.contains("pads") && fp["pads"].is_array()) {
        for (const auto& pad : fp["pads"]) {
            if (!pad.is_object() || !pad.contains("net") || pad["net"].is_null()) continue;
            std::string net = pad["net"].is_string() ? pad["net"].get<std::string>()
                                                     : pad["net"].dump();
            if (net_duty.contains(net)) {
                const nlohmann::json& rec = net_duty[net];
                if (!rec["duty"].is_null())
                    return {rec["duty"],
                            "gpio_duty:" + net + " (" + rec.value("source", std::string()) + ")"};
                return {1.0, "assumed_active (net " + net + " observed but duty unknown: " +
                                 rec.value("source", std::string()) + ")"};
            }
        }
    }
    return {1.0, "assumed_active (no observed activity mapping for this component)"};
}

}  // namespace

nlohmann::json build_power_profile(const nlohmann::json& trace, const nlohmann::json& board,
                                   const std::string& mcu_family_in,
                                   const nlohmann::json& trace_ref, double window_ms) {
    auto [gpio_events, uart_events] = split_events(trace);
    nlohmann::json gpio_final;
    if (trace.contains("gpio_final") && trace["gpio_final"].is_object())
        gpio_final = trace["gpio_final"];
    else if (trace.contains("gpio_states") && trace["gpio_states"].is_object())
        gpio_final = trace["gpio_states"];
    else
        gpio_final = nlohmann::json::object();
    nlohmann::json window = run_window_ms(gpio_events, uart_events, window_ms);
    nlohmann::json cpu = cpu_residency(uart_events, window);
    nlohmann::json pins = gpio_duty_cycles(gpio_events, gpio_final, window);

    nlohmann::json notes = nlohmann::json::array({kPowerModelNote});
    if (window.is_null()) {
        notes.push_back("no time-stamped events in trace — duty cycles limited to "
                        "final-state/assumed values (run a scenario for CHUDWARE_T_MARK "
                        "timestamps, or use CHUDWARE_GPIO_TRACE/CHUDWARE_CPU_TRACE)");
    }

    nlohmann::json rails_out = nlohmann::json::array();
    nlohmann::json unknown_active = nlohmann::json::array();
    long components_total = 0, components_with_rails = 0;

    if (board.is_object()) {
        std::string family = mcu_family_in;
        if (family.empty()) {
            if (auto fam = select_mcu_family(board)) family = *fam;
        }
        auto pin_nets = pin_net_map(board, family);
        // observed trace pin ("PA5") -> board net -> duty record
        nlohmann::json net_duty = nlohmann::json::object();
        for (auto& [pin_name, rec] : pins.items()) {
            if (!net_duty.contains(pin_name)) net_duty[pin_name] = rec;
            if (auto pid = resolve_pin(pin_name, family)) {
                auto it = pin_nets.find(*pid);
                if (it != pin_nets.end() && !net_duty.contains(it->second))
                    net_duty[it->second] = rec;
            }
        }

        struct RailAgg {
            nlohmann::json i_avg_a = nullptr;
            nlohmann::json i_peak_a = nullptr;
            nlohmann::json voltage_v_nom = nullptr;
            std::vector<double> duties;
            nlohmann::json provenance = nlohmann::json::array();
            std::vector<std::string> unknown;
        };
        std::map<std::string, RailAgg> agg;

        if (board.contains("footprints") && board["footprints"].is_array()) {
            for (const auto& fp : board["footprints"]) {
                if (!fp.is_object()) continue;
                ++components_total;
                auto rails = eng_supply_rails(fp);
                if (rails.empty()) continue;
                ++components_with_rails;
                std::string ref = fp.value("ref", std::string("?"));
                bool is_mcu = component_family(fp).has_value();
                auto [duty_j, act_source] = component_activity(fp, is_mcu, cpu, net_duty);
                for (const auto& rail : rails) {
                    std::string name = rail["name"].is_string() ? rail["name"].get<std::string>()
                                                                : "";
                    if (name.empty()) continue;
                    std::string up = name;
                    std::transform(up.begin(), up.end(), up.begin(),
                                   [](unsigned char c) { return std::toupper(c); });
                    if (kGroundNames.count(up)) continue;
                    RailAgg& r = agg[name];
                    if (r.voltage_v_nom.is_null() && rail["voltage_v_nom"].is_number())
                        r.voltage_v_nom = rail["voltage_v_nom"].get<double>();
                    bool have_iact = rail["current_a_active"].is_number();
                    bool have_islp = rail["current_a_sleep"].is_number();
                    if (!have_iact) {
                        r.unknown.push_back(ref);
                        unknown_active.push_back(ref + ":" + name);
                        r.provenance.push_back(
                            ref + ": active current unknown (null) — excluded from sums");
                        continue;
                    }
                    double i_act = rail["current_a_active"].get<double>();
                    double d = duty_j.is_number() ? duty_j.get<double>() : 1.0;
                    double avg = d * i_act;
                    std::string slp_note;
                    if (have_islp) {
                        avg += (1.0 - d) * rail["current_a_sleep"].get<double>();
                    } else if (d < 1.0) {
                        slp_note = " (sleep current unknown — sleep term omitted, avg is a floor)";
                    }
                    r.i_avg_a = (r.i_avg_a.is_null() ? 0.0 : r.i_avg_a.get<double>()) + avg;
                    r.i_peak_a = (r.i_peak_a.is_null() ? 0.0 : r.i_peak_a.get<double>()) + i_act;
                    r.duties.push_back(d);
                    std::string prov = ref + ": duty " + fmt_g(d) + " via " + act_source +
                                       "; i_active " + fmt_g(i_act) + "A";
                    if (have_islp)
                        prov += ", i_sleep " + fmt_g(rail["current_a_sleep"].get<double>()) + "A";
                    prov += slp_note;
                    r.provenance.push_back(prov);
                }
            }
        }

        double window_d = window.is_number() ? window.get<double>() : 0.0;
        std::vector<double> periods;
        for (auto& [_, rec] : pins.items())
            if (rec["period_s"].is_number()) periods.push_back(rec["period_s"].get<double>());
        for (auto& [name, r] : agg) {
            nlohmann::json duty = nullptr;
            if (!r.duties.empty()) {
                double sum = 0.0;
                for (double d : r.duties) sum += d;
                duty = sum / r.duties.size();
            }
            nlohmann::json sched = nullptr;
            if (duty.is_number()) {
                sched = {{"duty", round_n(duty.get<double>(), 6)},
                         {"period_s", !periods.empty() ? nlohmann::json(round_n(periods[0], 9))
                                                       : nlohmann::json(1.0)}};
                if (periods.empty())
                    r.provenance.push_back(
                        "schedule.period_s defaulted to 1.0s (no measured pin period)");
            }
            double d = duty.is_number() ? duty.get<double>() : 0.0;
            rails_out.push_back({
                {"name", name},
                {"voltage_v_nom", r.voltage_v_nom},
                {"i_avg_a", r.i_avg_a.is_null() ? nlohmann::json(nullptr)
                                                : nlohmann::json(round_n(r.i_avg_a.get<double>(), 9))},
                {"i_peak_a", r.i_peak_a.is_null()
                                 ? nlohmann::json(nullptr)
                                 : nlohmann::json(round_n(r.i_peak_a.get<double>(), 9))},
                {"residency",
                 {{"active_frac", duty.is_number() ? nlohmann::json(round_n(d, 6))
                                                   : nlohmann::json(nullptr)},
                  {"sleep_frac", duty.is_number() ? nlohmann::json(round_n(1.0 - d, 6))
                                                  : nlohmann::json(nullptr)},
                  {"active_s", duty.is_number() && window.is_number()
                                   ? nlohmann::json(round_n(d * window_d / 1000.0, 6))
                                   : nlohmann::json(nullptr)},
                  {"sleep_s", duty.is_number() && window.is_number()
                                  ? nlohmann::json(round_n((1.0 - d) * window_d / 1000.0, 6))
                                  : nlohmann::json(nullptr)}}},
                {"schedule", sched},
                {"provenance", r.provenance},
            });
        }
        if (rails_out.empty()) {
            notes.push_back("board resolved but no component carries eng.supply.rails metadata "
                            "— rails empty; seed the library or add per-instance "
                            "sim_params.eng overrides");
        }
    } else {
        notes.push_back("no board metadata available (board_doc_ref missing or unresolvable) — "
                        "activity profile only, rails empty");
    }

    return {
        {"schema", kPowerProfileSchema},
        {"model", kPowerModelNote},
        {"generated_at",
         std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch())
             .count()},
        {"trace_ref", trace_ref},
        {"window_ms", window},
        {"cpu", cpu},
        {"pins", pins},
        {"rails", rails_out},
        {"coverage",
         {{"components_total", components_total},
          {"components_with_rails", components_with_rails},
          {"unknown_active_current", unknown_active},
          {"notes", notes}}},
    };
}

std::optional<std::string> write_profile_artifact(const nlohmann::json& profile,
                                                  const std::string& elf_path) {
    try {
        namespace fs = std::filesystem;
        std::error_code ec;
        if (!fs::exists(elf_path, ec)) return std::nullopt;
        fs::path out = elf_path;
        out += ".power.json";
        fs::path tmp = out;
        tmp += ".tmp";
        {
            std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
            f << profile.dump(2);
        }
        fs::rename(tmp, out, ec);
        if (ec) return std::nullopt;
        return out.string();
    } catch (...) {
        return std::nullopt;
    }
}

}  // namespace chudware::firmware
