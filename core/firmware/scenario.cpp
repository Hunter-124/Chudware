#include "firmware/scenario.h"

#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <regex>
#include <set>
#include <sstream>

#include "firmware/pyfmt.h"

namespace aicad::firmware {
namespace {

const std::regex kPinRe(R"(^P([A-K])(\d{1,2})$)");

// Memory-mapped ADC stub windows (Python peripheral) per family — upstream
// Renode ships NO ADC model for any family in the T-FIN2 pack.
struct AdcStub {
    long long base;
    long long size;
    long long sr_offset;
    long long sr_value;
    long long dr_offset;
};
const std::map<std::string, AdcStub> kAdcStubs = {
    {"stm32f4", {0x40012000, 0x400, 0x00, 0x3, 0x4C}},
    {"stm32f1", {0x40012400, 0x400, 0x00, 0x3, 0x4C}},
};

const char* kTrackKinds[] = {"gpio_edges", "button", "quadrature", "i2c_sensor",
                             "i2c_registers", "i2c_register_stream", "spi_stub",
                             "uart_rx", "adc_waveform"};

std::string track_kinds_joined() {
    std::string out;
    for (size_t i = 0; i < sizeof(kTrackKinds) / sizeof(kTrackKinds[0]); ++i) {
        if (i) out += ", ";
        out += kTrackKinds[i];
    }
    return out;
}

bool is_num(const nlohmann::json& v) { return v.is_number(); }

std::optional<std::string> validate_pin(const nlohmann::json& pin) {
    if (!pin.is_string()) return "pin must be STM32-style 'P<A-K><0-15>', got " + py_repr(pin);
    std::string s = pin.get<std::string>();
    std::smatch m;
    if (!std::regex_match(s, m, kPinRe))
        return "pin must be STM32-style 'P<A-K><0-15>', got " + py_repr(pin);
    if (std::stoi(m[2].str()) > 15) return "pin number out of range in " + py_repr(pin);
    return std::nullopt;
}

std::vector<std::string> validate_i2c_samples(const nlohmann::json& samples,
                                              const std::string& kind) {
    std::vector<std::string> errs;
    if (!samples.is_array() || samples.empty()) {
        errs.push_back(kind + " needs a non-empty 'samples' list of {t_ms:number, "
                              "response:[byte]}");
        return errs;
    }
    bool have_prev = false;
    double prev_t = 0.0;
    int i = 0;
    for (const auto& s : samples) {
        std::string tag = kind + " samples[" + std::to_string(i) + "]";
        if (!s.is_object()) {
            errs.push_back(tag + " must be an object");
        } else {
            if (!is_num(s.value("t_ms", nlohmann::json()))) {
                errs.push_back(tag + " needs numeric 't_ms'");
            } else {
                double t = s["t_ms"].get<double>();
                if (have_prev && t < prev_t)
                    errs.push_back(tag + ".t_ms must be non-decreasing");
                prev_t = t;
                have_prev = true;
            }
            auto resp = s.find("response");
            bool ok = resp != s.end() && resp->is_array() && !resp->empty();
            if (ok) {
                for (const auto& b : *resp) {
                    if (!b.is_number_integer() || b.get<long long>() < 0 ||
                        b.get<long long>() > 255) {
                        ok = false;
                        break;
                    }
                }
            }
            if (!ok) errs.push_back(tag + " needs non-empty 'response' byte list (0-255)");
        }
        ++i;
    }
    return errs;
}

std::vector<std::string> validate_track(const nlohmann::json& tr) {
    std::vector<std::string> errs;
    std::string kind = tr.value("kind", std::string());
    if (kind == "gpio_edges") {
        if (auto e = validate_pin(tr.value("pin", nlohmann::json()))) errs.push_back(*e);
        auto evs = tr.find("events");
        if (evs == tr.end() || !evs->is_array() || evs->empty()) {
            errs.push_back("gpio_edges needs a non-empty 'events' list of {t_ms, level}");
        } else {
            for (const auto& ev : *evs) {
                if (!ev.is_object() || !is_num(ev.value("t_ms", nlohmann::json())) ||
                    !(ev.contains("level") && ev["level"].is_number_integer() &&
                      (ev["level"].get<long long>() == 0 || ev["level"].get<long long>() == 1))) {
                    errs.push_back("bad edge event " + py_repr(ev) +
                                   " (need {t_ms:number, level:0|1})");
                }
            }
        }
    } else if (kind == "button") {
        if (auto e = validate_pin(tr.value("pin", nlohmann::json()))) errs.push_back(*e);
        auto presses = tr.find("presses");
        if (presses == tr.end() || !presses->is_array() || presses->empty()) {
            errs.push_back("button needs a non-empty 'presses' list of {t_ms, hold_ms?}");
        } else {
            for (const auto& p : *presses) {
                if (!p.is_object() || !is_num(p.value("t_ms", nlohmann::json())))
                    errs.push_back("bad press " + py_repr(p) +
                                   " (need {t_ms:number, hold_ms?:number})");
            }
        }
    } else if (kind == "quadrature") {
        for (const char* key : {"pin_a", "pin_b"}) {
            if (auto e = validate_pin(tr.value(key, nlohmann::json())))
                errs.push_back(std::string(key) + ": " + *e);
        }
        if (!tr.contains("steps") || !tr["steps"].is_number_integer() ||
            tr["steps"].get<long long>() < 1)
            errs.push_back("quadrature needs integer 'steps' >= 1");
        if (!is_num(tr.value("step_ms", nlohmann::json())) || tr["step_ms"].get<double>() <= 0)
            errs.push_back("quadrature needs 'step_ms' > 0");
        if (tr.value("direction", std::string("cw")) != "cw" &&
            tr.value("direction", std::string("cw")) != "ccw")
            errs.push_back("quadrature 'direction' must be 'cw' or 'ccw'");
    } else if (kind == "i2c_sensor") {
        if (tr.value("model", std::string()) != "tmp103")
            errs.push_back("i2c_sensor model " + py_repr(tr.value("model", nlohmann::json())) +
                           " unsupported (supported: 'tmp103' — Renode's upstream Sensors.TMP103)");
        if (!tr.contains("address") || !tr["address"].is_number_integer())
            errs.push_back("i2c_sensor needs an integer 7-bit 'address'");
        if (!is_num(tr.value("temperature_c", nlohmann::json())))
            errs.push_back("i2c_sensor(tmp103) needs numeric 'temperature_c'");
    } else if (kind == "i2c_registers") {
        if (!tr.contains("address") || !tr["address"].is_number_integer())
            errs.push_back("i2c_registers needs an integer 7-bit 'address'");
        auto samples = tr.find("samples");
        auto resp = tr.find("response");
        if (samples != tr.end() && !samples->is_null()) {
            for (auto& e : validate_i2c_samples(*samples, kind)) errs.push_back(e);
        } else {
            bool ok = resp != tr.end() && resp->is_array() && !resp->empty();
            if (ok) {
                for (const auto& b : *resp) {
                    if (!b.is_number_integer() || b.get<long long>() < 0 ||
                        b.get<long long>() > 255) {
                        ok = false;
                        break;
                    }
                }
            }
            if (!ok)
                errs.push_back("i2c_registers needs a non-empty 'response' byte list (0-255) "
                               "or a non-empty 'samples' list of {t_ms, response}");
        }
    } else if (kind == "i2c_register_stream") {
        if (!tr.contains("address") || !tr["address"].is_number_integer())
            errs.push_back("i2c_register_stream needs an integer 7-bit 'address'");
        auto samples = tr.find("samples");
        bool have_samples = samples != tr.end() && !samples->is_null();
        if (!have_samples && tr.contains("response") && tr["response"].is_array()) {
            // allow a single static response coerced to one sample at t=0
        } else {
            for (auto& e : validate_i2c_samples(have_samples ? *samples : nlohmann::json(),
                                                kind))
                errs.push_back(e);
        }
    } else if (kind == "spi_stub") {
        if (!tr.contains("bus") || !tr["bus"].is_string() || tr["bus"].get<std::string>().empty())
            errs.push_back("spi_stub needs the platform SPI peripheral name in 'bus' (e.g. 'spi1')");
    } else if (kind == "uart_rx") {
        auto evs = tr.find("events");
        if (evs == tr.end() || !evs->is_array() || evs->empty()) {
            errs.push_back("uart_rx needs a non-empty 'events' list of {t_ms, text}");
        } else {
            for (const auto& ev : *evs) {
                if (!ev.is_object() || !is_num(ev.value("t_ms", nlohmann::json())) ||
                    !ev.contains("text") || !ev["text"].is_string() ||
                    ev["text"].get<std::string>().empty())
                    errs.push_back("bad uart_rx event " + py_repr(ev) +
                                   " (need {t_ms:number, text:str})");
            }
        }
    } else if (kind == "adc_waveform") {
        auto wf = tr.find("waveform");
        if (wf == tr.end() || !wf->is_object()) {
            errs.push_back("adc_waveform needs a 'waveform' object");
        } else {
            std::string wtype = wf->value("type", std::string());
            if (wtype == "sine" || wtype == "ramp") {
                for (const char* key : {"min", "max", "period_ms"}) {
                    if (!wf->contains(key) || !(*wf)[key].is_number())
                        errs.push_back("waveform '" + wtype + "' needs numeric '" + key + "'");
                }
            } else if (wtype == "samples") {
                auto vals = wf->find("values");
                bool ok = vals != wf->end() && vals->is_array() && !vals->empty();
                if (ok)
                    for (const auto& v : *vals)
                        if (!v.is_number()) {
                            ok = false;
                            break;
                        }
                if (!ok)
                    errs.push_back("waveform 'samples' needs a non-empty numeric 'values' list");
            } else {
                errs.push_back("waveform type " + py_repr(wf->value("type", nlohmann::json())) +
                               " unsupported (sine | ramp | samples)");
            }
        }
    }
    return errs;
}

// ── fixtures library ────────────────────────────────────────────────────────

// 12-bit ADC counts for an NTC thermistor (beta model) in a divider with
// r_fixed to VCC, sensor to GND — linear heat-up t_start..t_end.
std::vector<int> thermistor_counts(double t_start_c = 20.0, double t_end_c = 60.0, int n = 32,
                                   double beta = 3950.0, double r0 = 10000.0,
                                   double t0_c = 25.0, double r_fixed = 10000.0) {
    std::vector<int> counts;
    for (int i = 0; i < n; ++i) {
        double t_c = t_start_c + (t_end_c - t_start_c) * i / (n - 1);
        double t_k = t_c + 273.15, t0_k = t0_c + 273.15;
        double r = r0 * std::exp(beta * (1.0 / t_k - 1.0 / t0_k));
        counts.push_back(std::max(0, std::min(4095, static_cast<int>(
                              std::lround(4095.0 * r / (r + r_fixed))))));
    }
    return counts;
}

std::map<std::string, nlohmann::json> build_fixture_table() {
    std::map<std::string, nlohmann::json> fx;

    fx["i2c_temp_tmp103"] = {
        {"schema", kScenarioSchema},
        {"name", "I2C temperature sensor (TMP103 @ 0x70, 25.5 °C)"},
        {"tracks", nlohmann::json::array({{{"kind", "i2c_sensor"},
                                           {"model", "tmp103"},
                                           {"bus", "i2c1"},
                                           {"address", 0x70},
                                           {"temperature_c", 25.5}}})},
    };
    fx["spi_imu_stub"] = {
        {"schema", kScenarioSchema},
        {"name", "SPI IMU connectivity stub on spi1"},
        {"tracks", nlohmann::json::array(
                      {{{"kind", "spi_stub"}, {"bus", "spi1"}, {"name", "aicadImu"}}})},
    };
    fx["quadrature_encoder"] = {
        {"schema", kScenarioSchema},
        {"name", "Quadrature encoder PA0/PA1 — 24 steps CW @ 5 ms/step"},
        {"tracks", nlohmann::json::array({{{"kind", "quadrature"},
                                           {"pin_a", "PA0"},
                                           {"pin_b", "PA1"},
                                           {"start_ms", 100.0},
                                           {"steps", 24},
                                           {"step_ms", 5.0},
                                           {"direction", "cw"}}})},
    };
    fx["adc_thermistor"] = {
        {"schema", kScenarioSchema},
        {"name", "ADC thermistor heat-up 20→60 °C (10k NTC beta=3950, 10k divider)"},
        {"tracks",
         nlohmann::json::array({{{"kind", "adc_waveform"},
                                 {"channel", 0},
                                 {"sample_ms", 10.0},
                                 {"waveform",
                                  {{"type", "samples"}, {"values", thermistor_counts()}}}}})},
    };
    fx["user_button"] = {
        {"schema", kScenarioSchema},
        {"name", "User button on PA0 — three 80 ms presses"},
        {"tracks",
         nlohmann::json::array({{{"kind", "button"},
                                 {"pin", "PA0"},
                                 {"presses",
                                  nlohmann::json::array({{{"t_ms", 200.0}, {"hold_ms", 80.0}},
                                                         {{"t_ms", 600.0}, {"hold_ms", 80.0}},
                                                         {{"t_ms", 1000.0}, {"hold_ms", 80.0}}})}}})},
    };
    return fx;
}

const std::map<std::string, nlohmann::json>& fixture_table() {
    static const std::map<std::string, nlohmann::json> kFixtures = build_fixture_table();
    return kFixtures;
}

// ── Renode compilation helpers ───────────────────────────────────────────────

std::string btn_name(const std::string& pin) { return "aicadBtn" + pin; }

struct QuadratureEdge {
    double t_ms;
    std::string pin;
    int level;
};

// x4 Gray sequence — CW: (A,B) 00→10→11→01→…, CCW reversed.
std::vector<QuadratureEdge> quadrature_edges(const nlohmann::json& track) {
    const std::pair<int, int> phases_cw[4] = {{0, 0}, {1, 0}, {1, 1}, {0, 1}};
    std::vector<std::pair<int, int>> phases(std::begin(phases_cw), std::end(phases_cw));
    if (track.value("direction", std::string("cw")) != "cw")
        std::reverse(phases.begin(), phases.end());
    double start = track.value("start_ms", 0.0);
    double step_ms = track["step_ms"].get<double>();
    std::string pa = track["pin_a"].get<std::string>();
    std::string pb = track["pin_b"].get<std::string>();
    int steps = track["steps"].get<int>();
    std::vector<QuadratureEdge> edges;
    auto prev = phases[0];
    for (int i = 1; i <= steps; ++i) {
        auto cur = phases[i % 4];
        double t = start + i * step_ms;
        if (cur.first != prev.first) edges.push_back({t, pa, cur.first});
        if (cur.second != prev.second) edges.push_back({t, pb, cur.second});
        prev = cur;
    }
    return edges;
}

const char* kAdcScriptTemplate = R"SCRIPT(# AI-CAD ADC stub (aicad.fwscenario/1) — Python.PythonPeripheral script.
# Feeds programmed samples through the family's real ADC register window:
# SR read returns the ready flags; each DR read pops the next sample (cycling).
# Renode 1.16.1 request API: IsInit/IsRead/IsWrite/Offset/Value (capitalized).
if request.IsInit:
    samples = {SAMPLES}
    idx = 0
elif request.IsRead:
    if request.Offset == {SR_OFFSET}:
        request.Value = {SR_VALUE}
    elif request.Offset == {DR_OFFSET}:
        request.Value = samples[idx % len(samples)]
        idx = idx + 1
    else:
        request.Value = 0
)SCRIPT";

void replace_all(std::string& s, const std::string& from, const std::string& to) {
    size_t pos = 0;
    while ((pos = s.find(from, pos)) != std::string::npos) {
        s.replace(pos, from.size(), to);
        pos += to.size();
    }
}

std::string py_list_ints(const std::vector<int>& v) {
    // Python list repr: [1, 2, 3]
    std::string out = "[";
    for (size_t i = 0; i < v.size(); ++i) {
        if (i) out += ", ";
        out += std::to_string(v[i]);
    }
    out += "]";
    return out;
}

}  // namespace

std::vector<std::string> list_fixtures() {
    std::vector<std::string> out;
    for (const auto& [name, _] : fixture_table()) out.push_back(name);
    std::sort(out.begin(), out.end());
    return out;
}

nlohmann::json get_fixture(const std::string& name) {
    auto it = fixture_table().find(name);
    return it != fixture_table().end() ? it->second : nlohmann::json();
}

std::pair<nlohmann::json, std::vector<std::string>> load_scenario(
    const nlohmann::json& raw) {
    if (raw.is_null() || (raw.is_string() && raw.get<std::string>().empty()))
        return {nlohmann::json(), {}};
    if (raw.is_object()) return {raw, validate_scenario(raw)};
    if (raw.is_string()) {
        std::string s = raw.get<std::string>();
        // strip
        size_t b = s.find_first_not_of(" \t\r\n");
        s = b == std::string::npos ? "" : s.substr(b, s.find_last_not_of(" \t\r\n") - b + 1);
        if (s.empty()) return {nlohmann::json(), {}};
        if (s[0] != '{') {
            std::string name = s.rfind("fixture:", 0) == 0 ? s.substr(8) : s;
            nlohmann::json fx = get_fixture(name);
            if (fx.is_null()) {
                std::string avail;
                for (const auto& f : list_fixtures()) {
                    if (!avail.empty()) avail += ", ";
                    avail += f;
                }
                return {nlohmann::json(), {"unknown scenario fixture '" + name +
                                           "' (available: " + avail + ")"}};
            }
            return {fx, validate_scenario(fx)};
        }
        nlohmann::json obj = nlohmann::json::parse(s, nullptr, false);
        if (obj.is_discarded())
            return {nlohmann::json(), {"scenario is not valid JSON"}};
        if (!obj.is_object())
            return {nlohmann::json(), {"scenario JSON must be an object"}};
        return {obj, validate_scenario(obj)};
    }
    return {nlohmann::json(), {"scenario must be a dict, JSON string or fixture name"}};
}

std::vector<std::string> validate_scenario(const nlohmann::json& sc) {
    std::vector<std::string> errors;
    if (!sc.is_object() || sc.value("schema", std::string()) != kScenarioSchema) {
        errors.push_back(std::string("schema must be '") + kScenarioSchema + "', got " +
                         py_repr(sc.is_object() ? sc.value("schema", nlohmann::json())
                                                : nlohmann::json()));
    }
    if (!sc.is_object() || !sc.contains("tracks") || !sc["tracks"].is_array() ||
        sc["tracks"].empty()) {
        errors.push_back("scenario must carry a non-empty 'tracks' list");
        return errors;
    }
    int i = 0;
    for (const auto& tr : sc["tracks"]) {
        std::string tag = "tracks[" + std::to_string(i) + "]";
        if (!tr.is_object()) {
            errors.push_back(tag + ": must be an object");
        } else {
            std::string kind = tr.value("kind", std::string());
            bool known = false;
            for (const char* k : kTrackKinds) known = known || kind == k;
            if (!known) {
                errors.push_back(tag + ": unknown kind " +
                                 py_repr(tr.value("kind", nlohmann::json())) + " (one of " +
                                 track_kinds_joined() + ")");
            } else {
                for (auto& e : validate_track(tr)) errors.push_back(tag + ": " + e);
            }
        }
        ++i;
    }
    return errors;
}

std::vector<int> waveform_samples(const nlohmann::json& wf, double sample_ms, int n) {
    auto clamp = [](double v) {
        return std::max(0, std::min(4095, static_cast<int>(std::lround(v))));
    };
    std::vector<int> out;
    if (wf.value("type", std::string()) == "samples") {
        for (const auto& v : wf["values"]) out.push_back(clamp(v.get<double>()));
        return out;
    }
    double lo = wf["min"].get<double>(), hi = wf["max"].get<double>();
    double period = wf["period_ms"].get<double>();
    int count = std::max(2, std::min(n, static_cast<int>(std::lround(
                          period / std::max(sample_ms, 1e-6)))));
    for (int i = 0; i < count; ++i) {
        double ph = static_cast<double>(i) / count;
        double v;
        if (wf.value("type", std::string()) == "sine")
            v = lo + (hi - lo) * (0.5 + 0.5 * std::sin(2 * M_PI * ph));
        else  // ramp
            v = lo + (hi - lo) * ph;
        out.push_back(clamp(v));
    }
    return out;
}

ScenarioPlan build_scenario_plan(const nlohmann::json& scenario, const nlohmann::json& caps,
                                 const std::string& platform_text) {
    ScenarioPlan plan;
    plan.errors = validate_scenario(scenario);
    if (!plan.errors.empty()) return plan;

    std::string family = caps.value("family", std::string("?"));
    std::string caps_file = caps.value("file", std::string());
    std::string caps_notes = caps.value("notes", std::string());

    auto gpio_port_ok = [&platform_text](const std::string& pin) {
        std::smatch m;
        return std::regex_match(pin, m, kPinRe) &&
               platform_text.find("gpioPort" + m[1].str()) != std::string::npos;
    };

    auto add_gpio_pin = [&](const std::string& pin) -> bool {
        if (!caps.value("gpio", false) || !gpio_port_ok(pin)) {
            std::smatch m;
            std::regex_match(pin, m, kPinRe);
            std::string port = m.ready() && m.size() > 1 ? m[1].str() : std::string(1, pin.size() > 1 ? pin[1] : '?');
            plan.errors.push_back(
                "gpio stimulus on " + pin + " unsupported: platform '" + caps_file +
                "' (family " + family + ") does not declare gpioPort" + port + " — " +
                caps_notes);
            return false;
        }
        std::smatch m;
        std::regex_match(pin, m, kPinRe);
        std::string port = m[1].str();
        int num = std::stoi(m[2].str());
        std::string line = "machine LoadPlatformDescriptionFromString \"" + btn_name(pin) +
                           ": Miscellaneous.Button @ gpioPort" + port + " " +
                           std::to_string(num) + " { -> gpioPort" + port + "@" +
                           std::to_string(num) + " }\"";
        if (std::find(plan.setup_lines.begin(), plan.setup_lines.end(), line) ==
            plan.setup_lines.end())
            plan.setup_lines.push_back(line);
        return true;
    };

    auto edge_line = [](const std::string& pin, int level) {
        std::smatch m;
        std::regex_match(pin, m, kPinRe);
        return "sysbus.gpioPort" + m[1].str() + "." + btn_name(pin) +
               (level ? " Press" : " Release");
    };

    int i2c_seq = 0;
    for (const auto& tr : scenario["tracks"]) {
        std::string kind = tr.value("kind", std::string());
        if (kind == "gpio_edges") {
            if (add_gpio_pin(tr["pin"].get<std::string>())) {
                for (const auto& ev : tr["events"]) {
                    plan.timeline.push_back({ev["t_ms"].get<double>(),
                                             edge_line(tr["pin"].get<std::string>(),
                                                       ev["level"].get<int>())});
                }
            }
        } else if (kind == "button") {
            if (add_gpio_pin(tr["pin"].get<std::string>())) {
                for (const auto& p : tr["presses"]) {
                    double t = p["t_ms"].get<double>();
                    double hold = p.value("hold_ms", 50.0);
                    plan.timeline.push_back({t, edge_line(tr["pin"].get<std::string>(), 1)});
                    plan.timeline.push_back(
                        {t + hold, edge_line(tr["pin"].get<std::string>(), 0)});
                }
            }
        } else if (kind == "quadrature") {
            if (add_gpio_pin(tr["pin_a"].get<std::string>()) &&
                add_gpio_pin(tr["pin_b"].get<std::string>())) {
                for (const auto& e : quadrature_edges(tr))
                    plan.timeline.push_back({e.t_ms, edge_line(e.pin, e.level)});
            }
        } else if (kind == "i2c_sensor" || kind == "i2c_registers" ||
                   kind == "i2c_register_stream") {
            std::string bus = tr.value("bus", std::string("i2c1"));
            if (!caps.value("i2c", false) ||
                platform_text.find(bus + ":") == std::string::npos) {
                plan.errors.push_back(
                    "i2c stimulus unsupported: platform '" + caps_file + "' (family " + family +
                    ") does not declare I2C bus '" + bus + "' — " + caps_notes);
                continue;
            }
            long long addr = tr["address"].get<long long>();
            if (kind == "i2c_sensor") {
                std::string name = "aicadTemp" + std::to_string(i2c_seq);
                plan.setup_lines.push_back("machine LoadPlatformDescriptionFromString \"" + name +
                                           ": Sensors.TMP103 @ " + bus + " " + fmt_hex(addr) +
                                           "\"");
                plan.setup_lines.push_back("sysbus." + bus + "." + name + " Temperature " +
                                           fmt_g(tr["temperature_c"].get<double>()));
            } else {
                std::string name = "aicadI2C" + std::to_string(i2c_seq);
                plan.setup_lines.push_back("machine LoadPlatformDescriptionFromString \"" + name +
                                           ": Mocks.DummyI2CSlave @ " + bus + " " + fmt_hex(addr) +
                                           "\"");
                nlohmann::json samples;
                if (tr.contains("samples") && tr["samples"].is_array())
                    samples = tr["samples"];
                else if (kind == "i2c_register_stream" && tr.contains("response") &&
                         tr["response"].is_array()) {
                    samples = nlohmann::json::array(
                        {{{"t_ms", 0.0}, {"response", tr["response"]}}});
                }
                if (samples.is_array() && !samples.empty()) {
                    // Seed with the first sample at setup so a read before t0 works.
                    for (const auto& b : samples[0]["response"])
                        plan.setup_lines.push_back("sysbus." + bus + "." + name +
                                                   " EnqueueResponseByte " +
                                                   fmt_hex(b.get<long long>()));
                    int si = 0;
                    for (const auto& s : samples) {
                        double t = s["t_ms"].get<double>();
                        if (si == 0 && t <= 0.0) {
                            ++si;
                            continue;
                        }
                        plan.timeline.push_back(
                            {t, "echo \"AICAD_I2C_SAMPLE " + bus + " " + fmt_hex(addr) +
                                    " t_ms=" + fmt_g(t) + "\""});
                        for (const auto& b : s["response"])
                            plan.timeline.push_back({t, "sysbus." + bus + "." + name +
                                                            " EnqueueResponseByte " +
                                                            fmt_hex(b.get<long long>())});
                        ++si;
                    }
                } else {
                    for (const auto& b : tr["response"])
                        plan.setup_lines.push_back("sysbus." + bus + "." + name +
                                                   " EnqueueResponseByte " +
                                                   fmt_hex(b.get<long long>()));
                }
            }
            ++i2c_seq;
        } else if (kind == "spi_stub") {
            std::string bus = tr["bus"].get<std::string>();
            if (!caps.value("spi", false) ||
                platform_text.find(bus + ":") == std::string::npos) {
                plan.errors.push_back(
                    "spi stimulus unsupported: platform '" + caps_file + "' (family " + family +
                    ") does not declare SPI bus '" + bus + "' — " + caps_notes);
                continue;
            }
            std::string name = tr.value("name", std::string("aicadSpiStub"));
            plan.setup_lines.push_back("machine LoadPlatformDescriptionFromString \"" + name +
                                       ": Mocks.DummySPISlave @ " + bus + "\"");
            plan.warnings.push_back(
                "spi_stub '" + name +
                "': DummySPISlave answers every frame with a fixed pattern (connectivity stub) — "
                "programmable SPI register models have no documented upstream Renode idiom yet");
        } else if (kind == "uart_rx") {
            std::string uart;
            if (tr.contains("uart") && tr["uart"].is_string()) {
                uart = tr["uart"].get<std::string>();
            } else {
                static const std::regex uartRe(R"(^(\w+)\s*:\s*UART\.)",
                                               std::regex_constants::ECMAScript);
                // multiline search over the platform text
                std::string text = platform_text;
                std::istringstream in(text);
                std::string ln;
                while (std::getline(in, ln)) {
                    std::smatch m;
                    if (std::regex_search(ln, m, uartRe)) {
                        uart = m[1].str();
                        break;
                    }
                }
            }
            if (uart.empty() || !caps.value("uart", false)) {
                plan.errors.push_back(
                    "uart_rx stimulus unsupported: platform '" + caps_file + "' (family " +
                    family + ") declares no UART — " + caps_notes);
                continue;
            }
            for (const auto& ev : tr["events"]) {
                double t = ev["t_ms"].get<double>();
                for (unsigned char ch : ev["text"].get<std::string>())
                    plan.timeline.push_back(
                        {t, "sysbus." + uart + " WriteChar " + fmt_hex(ch)});
            }
        } else if (kind == "adc_waveform") {
            auto it = kAdcStubs.find(family);
            if (it == kAdcStubs.end()) {
                std::string registered;
                for (const auto& [f, _] : kAdcStubs) {
                    if (!registered.empty()) registered += ", ";
                    registered += f;
                }
                plan.errors.push_back(
                    "adc stimulus unsupported on family '" + family +
                    "': upstream Renode ships no ADC model for these parts and no "
                    "Python-peripheral ADC stub window is registered for this family "
                    "(registered: " + registered + ")");
                continue;
            }
            const AdcStub& stub = it->second;
            std::vector<int> samples =
                waveform_samples(tr["waveform"], tr.value("sample_ms", 1.0));
            std::string script = kAdcScriptTemplate;
            replace_all(script, "{SAMPLES}", py_list_ints(samples));
            replace_all(script, "{SR_OFFSET}", fmt_hex(stub.sr_offset));
            replace_all(script, "{SR_VALUE}", fmt_hex(stub.sr_value));
            replace_all(script, "{DR_OFFSET}", fmt_hex(stub.dr_offset));
            // Aux .repl so the triple-quoted script survives intact.
            std::string repl_name = "aicad_adc_stub.repl";
            plan.aux_files[repl_name] =
                "aicadAdc: Python.PythonPeripheral @ sysbus " + fmt_hex(stub.base) + "\n"
                "    size: " + fmt_hex(stub.size) + "\n"
                "    initable: true\n"
                "    script: '''\n" + script + "'''\n";
            plan.setup_lines.push_back("machine LoadPlatformDescription "
                                       "@{AICAD_SCENARIO_DIR}/" + repl_name);
        }
    }

    std::stable_sort(plan.timeline.begin(), plan.timeline.end(),
                     [](const auto& a, const auto& b) { return a.first < b.first; });
    if (!plan.timeline.empty()) plan.duration_ms = plan.timeline.back().first;
    return plan;
}

std::vector<std::string> timeline_resc_lines(
    const std::vector<std::pair<double, std::string>>& timeline, double run_seconds) {
    std::vector<std::string> lines = {"echo \"AICAD_T_MARK 0\""};
    double now_ms = 0.0;
    for (const auto& [t_ms, action] : timeline) {
        double dt = std::max(0.0, t_ms - now_ms);
        if (dt > 0) {
            lines.push_back("sleep " + fmt_3f(dt / 1000.0));
            now_ms = t_ms;
            lines.push_back("echo \"AICAD_T_MARK " + fmt_g(now_ms) + "\"");
        }
        lines.push_back(action);
    }
    double remaining = run_seconds * 1000.0 - now_ms;
    if (remaining > 0) {
        lines.push_back("sleep " + fmt_3f(remaining / 1000.0));
        lines.push_back("echo \"AICAD_T_MARK " + fmt_g(run_seconds * 1000.0) + "\"");
    }
    return lines;
}

void materialize_aux_files(ScenarioPlan& plan, const std::string& directory) {
    namespace fs = std::filesystem;
    std::error_code ec;
    fs::create_directories(directory, ec);
    for (const auto& [name, content] : plan.aux_files) {
        std::ofstream out(fs::path(directory) / name, std::ios::binary | std::ios::trunc);
        out << content;
    }
    for (auto& ln : plan.setup_lines) replace_all(ln, "{AICAD_SCENARIO_DIR}", directory);
}

}  // namespace aicad::firmware
