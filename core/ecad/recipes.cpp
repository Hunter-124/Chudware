// Parameterized known-good ECAD designs — see recipes.h. Faithful port of
// engine/ecad/recipes.py (value formatting + the four recipes + closest_recipe).

#include "ecad/recipes.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <regex>
#include <string>
#include <vector>

#include "ecad/ops.h"  // OpError

namespace chudware::ecad::recipes {

using nlohmann::json;

namespace {

constexpr double kPi = 3.14159265358979323846;

// engine/ecad/recipes.py::_trim — compact numeric string via f"{x:.4g}".
// Python's format mini-language 'g' == C printf %.4g (C99), so %g matches.
std::string trim(double x) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.4g", x);
    return std::string(buf);
}

// engine/ecad/recipes.py::_fmt_ohms
std::string fmt_ohms(double r) {
    if (r >= 1e6) return trim(r / 1e6) + "Meg";
    if (r >= 1e3) return trim(r / 1e3) + "k";
    return trim(r);
}

// engine/ecad/recipes.py::_fmt_farads
std::string fmt_farads(double c) {
    if (c >= 1e-3) return trim(c / 1e-3) + "m";
    if (c >= 1e-6) return trim(c / 1e-6) + "u";
    if (c >= 1e-9) return trim(c / 1e-9) + "n";
    return trim(c / 1e-12) + "p";
}

std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return std::tolower(c); });
    return s;
}

}  // namespace

json voltage_divider(double vin, double vout) {
    vin = (double)vin;
    if (vin != 0.0)
        vout = std::max(std::min((double)vout, vin * 0.999), vin * 0.001);
    else
        vout = 0.0;
    double rtotal = 20000.0;
    double ratio = (vin != 0.0) ? (vout / vin) : 0.5;
    double r2 = rtotal * ratio;
    double r1 = rtotal - r2;
    return json{
        {"name", "voltage_divider"},
        {"request", "a " + trim(vin) + "V resistive divider with " + trim(vout) + "V output"},
        {"components", json::array({
            json{{"ref", "V1"}, {"libid", "Simulation_SPICE:V_source"}, {"value", trim(vin)}},
            json{{"ref", "R1"}, {"libid", "Device:R"}, {"value", fmt_ohms(r1)}},
            json{{"ref", "R2"}, {"libid", "Device:R"}, {"value", fmt_ohms(r2)}},
        })},
        {"nets", json{
            {"IN", json::array({json{{"ref", "V1"}, {"pin", "+"}}, json{{"ref", "R1"}, {"pin", "1"}}})},
            {"VMID", json::array({json{{"ref", "R1"}, {"pin", "2"}}, json{{"ref", "R2"}, {"pin", "1"}}})},
            {"GND", json::array({json{{"ref", "V1"}, {"pin", "-"}}, json{{"ref", "R2"}, {"pin", "2"}}})},
        }},
        {"study", json{
            {"analysis", "op"},
            {"params", json::object()},
            {"expect", json{{"probe", "V(VMID)"}, {"value", vout}, {"tol", 0.02}}},
        }},
        {"notes", "R1=" + fmt_ohms(r1) + " (top), R2=" + fmt_ohms(r2) + " (bottom); VMID = " +
                      trim(vin) + " * R2/(R1+R2) = " + trim(vout) + " V."},
    };
}

json rc_lowpass(double fc) {
    double r = 1000.0;
    double c = (fc > 0.0) ? 1.0 / (2.0 * kPi * fc * r) : 1e-7;
    return json{
        {"name", "rc_lowpass"},
        {"request", "an RC low-pass filter with a " + trim(fc) + "Hz cutoff"},
        {"components", json::array({
            json{{"ref", "V1"}, {"libid", "Simulation_SPICE:V_source"}, {"value", "0"}, {"ac_mag", 1.0}},
            json{{"ref", "R1"}, {"libid", "Device:R"}, {"value", fmt_ohms(r)}},
            json{{"ref", "C1"}, {"libid", "Device:C"}, {"value", fmt_farads(c)}},
        })},
        {"nets", json{
            {"IN", json::array({json{{"ref", "V1"}, {"pin", "+"}}, json{{"ref", "R1"}, {"pin", "1"}}})},
            {"OUT", json::array({json{{"ref", "R1"}, {"pin", "2"}}, json{{"ref", "C1"}, {"pin", "1"}}})},
            {"GND", json::array({json{{"ref", "V1"}, {"pin", "-"}}, json{{"ref", "C1"}, {"pin", "2"}}})},
        }},
        {"study", json{
            {"analysis", "ac"},
            {"params", json{{"sweep", "dec"}, {"points", 20},
                            {"fstart", std::max(fc / 100.0, 0.1)}, {"fstop", fc * 100.0},
                            {"ac_source", "V1"}, {"ac_mag", 1.0}}},
            {"expect", json{{"probe", "V(OUT)"}, {"value", 1.0 / std::sqrt(2.0)},
                            {"tol", 0.02}, {"at_freq", fc}}},
        }},
        {"notes", "R=" + fmt_ohms(r) + ", C=" + fmt_farads(c) + "; fc = 1/(2*pi*R*C) = " +
                      trim(fc) + " Hz, |H(fc)| = 0.707 (-3 dB)."},
    };
}

json inverting_amp(double gain) {
    gain = std::abs(gain);
    double rin = 1000.0;
    double rf = gain * rin;
    double vin = 0.2;
    double vout = -gain * vin;
    return json{
        {"name", "inverting_amp"},
        {"request", "an inverting amplifier with a gain of " + trim(gain)},
        {"components", json::array({
            json{{"ref", "V1"}, {"libid", "Simulation_SPICE:V_source"}, {"value", trim(vin)}},
            json{{"ref", "Rin"}, {"libid", "Device:R"}, {"value", fmt_ohms(rin)}},
            json{{"ref", "Rf"}, {"libid", "Device:R"}, {"value", fmt_ohms(rf)}},
            json{{"ref", "U1"}, {"libid", "Amplifier_Operational:OPAMP_dual"}, {"value", "OPAMP"}},
        })},
        {"nets", json{
            {"IN", json::array({json{{"ref", "V1"}, {"pin", "+"}}, json{{"ref", "Rin"}, {"pin", "1"}}})},
            {"INV", json::array({json{{"ref", "Rin"}, {"pin", "2"}}, json{{"ref", "Rf"}, {"pin", "1"}},
                                 json{{"ref", "U1"}, {"pin", "1"}}})},
            {"OUT", json::array({json{{"ref", "Rf"}, {"pin", "2"}}, json{{"ref", "U1"}, {"pin", "3"}}})},
            {"GND", json::array({json{{"ref", "V1"}, {"pin", "-"}}, json{{"ref", "U1"}, {"pin", "2"}}})},
        }},
        {"study", json{
            {"analysis", "op"},
            {"params", json::object()},
            {"expect", json{{"probe", "V(OUT)"}, {"value", vout}, {"tol", 0.02}}},
        }},
        {"notes", "Rin=" + fmt_ohms(rin) + ", Rf=" + fmt_ohms(rf) + "; gain = -Rf/Rin = -" +
                      trim(gain) + "; Vin=" + trim(vin) + " V => Vout=" + trim(vout) + " V."},
    };
}

json rc_555_astable(double freq) {
    double c = 1e-6;
    double r = (freq > 0.0) ? 1.44 / (3.0 * freq * c) : 1e5;
    double period = (freq > 0.0) ? 1.0 / freq : 1.0;
    return json{
        {"name", "rc_555_astable"},
        {"request", "a 555 astable oscillator at about " + trim(freq) + "Hz"},
        {"components", json::array({
            json{{"ref", "V1"}, {"libid", "Simulation_SPICE:V_source"}, {"value", "5"}},
            json{{"ref", "U1"}, {"libid", "Timer:NE555"}, {"value", "NE555"}},
            json{{"ref", "Ra"}, {"libid", "Device:R"}, {"value", fmt_ohms(r)}},
            json{{"ref", "Rb"}, {"libid", "Device:R"}, {"value", fmt_ohms(r)}},
            json{{"ref", "C1"}, {"libid", "Device:C"}, {"value", fmt_farads(c)}},
        })},
        {"nets", json{
            {"VCC", json::array({json{{"ref", "V1"}, {"pin", "+"}}, json{{"ref", "U1"}, {"pin", "8"}},
                                 json{{"ref", "U1"}, {"pin", "4"}}, json{{"ref", "Ra"}, {"pin", "1"}}})},
            {"DISCH", json::array({json{{"ref", "Ra"}, {"pin", "2"}}, json{{"ref", "Rb"}, {"pin", "1"}},
                                   json{{"ref", "U1"}, {"pin", "7"}}})},
            {"THR", json::array({json{{"ref", "Rb"}, {"pin", "2"}}, json{{"ref", "C1"}, {"pin", "1"}},
                                 json{{"ref", "U1"}, {"pin", "6"}}, json{{"ref", "U1"}, {"pin", "2"}}})},
            {"OUT", json::array({json{{"ref", "U1"}, {"pin", "3"}}})},
            {"GND", json::array({json{{"ref", "V1"}, {"pin", "-"}}, json{{"ref", "U1"}, {"pin", "1"}},
                                 json{{"ref", "C1"}, {"pin", "2"}}})},
        }},
        {"study", json{
            {"analysis", "tran"},
            {"params", json{{"tstep", period / 200.0}, {"tstop", period * 3.0}, {"uic", true}}},
            {"expect", json{{"probe", "V(OUT)"}, {"value", period}, {"tol", 0.25}, {"kind", "period"}}},
        }},
        {"notes", "Ra=Rb=" + fmt_ohms(r) + ", C=" + fmt_farads(c) + "; f ~ 1.44/((Ra+2Rb)*C) = " +
                      trim(freq) + " Hz, period " + trim(period) + " s."},
    };
}

// (name, list-of-keywords) — order matters for "closest" selection (mirrors RECIPES).
namespace {
struct RecipeMeta {
    std::string name;
    std::vector<std::string> keywords;
};
const std::vector<RecipeMeta>& recipe_metas() {
    static const std::vector<RecipeMeta> kMetas = {
        {"inverting_amp", {"inverting", "invert", "amplifier", "amp", "op-amp", "opamp", "gain"}},
        {"rc_555_astable", {"555", "astable", "oscillator", "timer", "blink", "square wave", "monostable"}},
        {"rc_lowpass", {"low-pass", "lowpass", "low pass", "filter", "rc", "cutoff", "corner"}},
        {"voltage_divider", {"divider", "voltage divider", "resistive", "divide", "potential"}},
    };
    return kMetas;
}

double get_param(const json& params, const char* key, double dflt) {
    if (params.is_object() && params.contains(key) && params[key].is_number())
        return params[key].get<double>();
    return dflt;
}
}  // namespace

json build(const std::string& name, const json& params) {
    json p = params.is_object() ? params : json::object();
    if (name == "inverting_amp") return inverting_amp(get_param(p, "gain", 10.0));
    if (name == "rc_555_astable") return rc_555_astable(get_param(p, "freq", 1.0));
    if (name == "rc_lowpass") return rc_lowpass(get_param(p, "fc", 1000.0));
    if (name == "voltage_divider") return voltage_divider(get_param(p, "vin", 5.0), get_param(p, "vout", 2.5));
    throw OpError("invalid_op", "no recipe named '" + name + "'", {{"reason", "unknown recipe"}});
}

json all_recipes() {
    json out = json::array();
    for (const auto& m : recipe_metas()) out.push_back(build(m.name, json::object()));
    return out;
}

namespace {
// engine/ecad/recipes.py::_extract_number
bool extract_number(const std::string& request, double& out) {
    std::string low = lower(request);
    static const std::vector<std::string> pats = {
        R"(gain\s*(?:of|=|:)?\s*([0-9]*\.?[0-9]+))",
        R"(([0-9]*\.?[0-9]+)\s*(?:hz|khz|hertz))",
        R"(([0-9]*\.?[0-9]+)\s*v\b)",
    };
    for (std::size_t i = 0; i < pats.size(); ++i) {
        std::smatch m;
        std::regex re(pats[i]);
        if (std::regex_search(low, m, re)) {
            double val = std::stod(m[1].str());
            // "if 'khz' in low and 'hz' in pat" — pat index 1 is the (hz|khz|hertz) one.
            if (i == 1 && low.find("khz") != std::string::npos) val *= 1000.0;
            out = val;
            return true;
        }
    }
    std::smatch m;
    std::regex re(R"(([0-9]*\.?[0-9]+))");
    if (std::regex_search(low, m, re)) {
        out = std::stod(m[1].str());
        return true;
    }
    return false;
}
}  // namespace

json closest_recipe(const std::string& request) {
    std::string low = lower(request);
    std::string best_name = "voltage_divider";
    int best_score = -1;
    for (const auto& m : recipe_metas()) {
        int score = 0;
        for (const auto& kw : m.keywords)
            if (low.find(kw) != std::string::npos) ++score;
        if (score > best_score) {
            best_score = score;
            best_name = m.name;
        }
    }

    double num = 0.0;
    bool has_num = extract_number(request, num);

    if (best_name == "voltage_divider") {
        // re.findall(r"([0-9]*\.?[0-9]+)\s*v\b", low)
        std::vector<double> vs;
        std::regex re(R"(([0-9]*\.?[0-9]+)\s*v\b)");
        for (auto it = std::sregex_iterator(low.begin(), low.end(), re); it != std::sregex_iterator(); ++it)
            vs.push_back(std::stod((*it)[1].str()));
        if (vs.size() >= 2) return voltage_divider(vs[0], vs[1]);
        if (vs.size() == 1) return voltage_divider(vs[0], vs[0] / 2.0);
        return voltage_divider();
    }
    if (best_name == "inverting_amp") return has_num ? inverting_amp(num) : inverting_amp();
    if (best_name == "rc_lowpass") return has_num ? rc_lowpass(num) : rc_lowpass();
    if (best_name == "rc_555_astable") return has_num ? rc_555_astable(num) : rc_555_astable();
    return voltage_divider();
}

}  // namespace chudware::ecad::recipes
