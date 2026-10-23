#include "firmware/coverify.h"

#include <algorithm>
#include <fstream>
#include <regex>
#include <set>
#include <sstream>

#include "ecad/netlist.h"
#include "ecad/schema.h"
#include "firmware/eng_rails.h"
#include "firmware/mcu_pinouts.h"
#include "firmware/pinmap.h"

namespace chudware::firmware {
namespace {

// Ground-ish rail names — supply rails that need no decoupling check.
const std::set<std::string> kGroundNames = {"GND", "GNDA", "GNDD", "AGND", "DGND", "PGND",
                                            "EGND", "VSS", "VSSA", "EARTH", "CHASSIS"};

nlohmann::json violation(const std::string& rule, const std::string& severity,
                         const nlohmann::json& net, std::vector<std::string> pins,
                         const std::string& message, const std::string& fix = "") {
    std::sort(pins.begin(), pins.end());
    return {{"rule", rule},   {"severity", severity}, {"net", net},
            {"pins", pins},   {"xy", nullptr},        {"message", message},
            {"fix", fix}};
}

// _sorted(): order by (rule, net or "", pins) — xy is always null here.
void sort_findings(nlohmann::json& findings) {
    std::sort(findings.begin(), findings.end(), [](const nlohmann::json& a, const nlohmann::json& b) {
        auto key = [](const nlohmann::json& v) {
            std::string net = v["net"].is_string() ? v["net"].get<std::string>() : "";
            return std::make_tuple(v.value("rule", std::string()), net,
                                   v["pins"].get<std::vector<std::string>>());
        };
        return key(a) < key(b);
    });
}

// ── firmware source scan ────────────────────────────────────────────────────

const std::regex kPinRefRe(R"(\bPIN_([A-Za-z0-9_]+)\b)");
const std::regex kWriteRe(
    R"(\b(?:HAL_GPIO_WritePin|HAL_GPIO_TogglePin|LL_GPIO_SetOutputPin|LL_GPIO_ResetOutputPin|)"
    R"(gpio_set|gpio_clear|gpio_write|gpio_toggle|gpio_put|digitalWrite|CHUDWARE_GPIO_TRACE)\b)");
const std::regex kReadRe(
    R"(\b(?:HAL_GPIO_ReadPin|LL_GPIO_ReadInputPort|gpio_read|gpio_get|digitalRead)\b)");
const std::regex kModeOutRe(
    R"(\b(?:pinMode|gpio_set_dir|gpio_mode)\s*\(\s*PIN_([A-Za-z0-9_]+)\s*,\s*)"
    R"((?:OUTPUT|GPIO_OUT|GPIO_MODE_OUTPUT\w*)\b)");
const std::regex kModeInRe(
    R"(\b(?:pinMode|gpio_set_dir|gpio_mode)\s*\(\s*PIN_([A-Za-z0-9_]+)\s*,\s*)"
    R"((?:INPUT\w*|GPIO_IN|GPIO_MODE_INPUT\w*)\b)");

// ── board-side indexes ──────────────────────────────────────────────────────

std::vector<nlohmann::json> mcu_footprints(const nlohmann::json& board) {
    std::vector<nlohmann::json> out;
    if (!board.is_object() || !board.contains("footprints") || !board["footprints"].is_array())
        return out;
    for (const auto& fp : board["footprints"]) {
        if (fp.is_object() && component_family(fp)) out.push_back(fp);
    }
    return out;
}

std::set<std::string> fp_nets(const nlohmann::json& fp) {
    std::set<std::string> out;
    if (fp.contains("pads") && fp["pads"].is_array()) {
        for (const auto& p : fp["pads"]) {
            if (p.is_object() && p.contains("net") && !p["net"].is_null())
                out.insert(p["net"].is_string() ? p["net"].get<std::string>()
                                                : p["net"].dump());
        }
    }
    return out;
}

bool is_capacitor(const nlohmann::json& fp) {
    std::string fpid = fp.value("fpid", std::string());
    std::string ref = fp.value("ref", std::string());
    if (fpid.find("Capacitor") != std::string::npos) return true;
    std::string last = fpid.substr(fpid.find_last_of(':') == std::string::npos
                                       ? 0
                                       : fpid.find_last_of(':') + 1);
    if (last.rfind("C_", 0) == 0 || last.rfind("CP_", 0) == 0) return true;
    static const std::regex cRef(R"(^C\d)");
    return std::regex_search(ref, cRef);
}

std::set<std::string> mcu_sch_refs(const nlohmann::json& sch) {
    std::set<std::string> out;
    if (!sch.is_object() || !sch.contains("symbols") || !sch["symbols"].is_array()) return out;
    for (const auto& s : sch["symbols"]) {
        if (s.is_object() && component_family(s))
            out.insert(s.value("ref", std::string()));
    }
    return out;
}

// Pinrefs of non-MCU push-pull drivers (output/power_out) on `net` per the
// linked schematic's netlist. Empty when no schematic (honest skip).
std::vector<std::string> external_strong_drivers(const nlohmann::json& sch,
                                                 const std::string& net,
                                                 const std::set<std::string>& mcu_refs) {
    std::vector<std::string> out;
    if (!sch.is_object() || !chudware::ecad::schema::is_schematic(sch)) return out;
    nlohmann::json nl = chudware::ecad::build_netlist(sch);
    if (!nl.contains("nets") || !nl["nets"].is_object() || !nl["nets"].contains(net)) return out;
    std::map<std::string, std::string> sym_ref;
    if (sch.contains("symbols") && sch["symbols"].is_array()) {
        for (const auto& s : sch["symbols"]) {
            if (s.is_object() && s.contains("id"))
                sym_ref[s["id"].is_string() ? s["id"].get<std::string>() : s["id"].dump()] =
                    s.value("ref", std::string());
        }
    }
    for (const auto& m : nl["nets"][net]) {
        if (!m.is_object()) continue;
        std::string type = m.value("type", std::string("passive"));
        if (type != "output" && type != "power_out") continue;
        std::string sym = m.contains("sym") && m["sym"].is_string() ? m["sym"].get<std::string>() : "";
        auto it = sym_ref.find(sym);
        if (it != sym_ref.end() && mcu_refs.count(it->second)) continue;
        std::string pin = m.contains("pin") && m["pin"].is_string() ? m["pin"].get<std::string>() : "";
        out.push_back(sym + "." + pin);
    }
    std::sort(out.begin(), out.end());
    return out;
}

// Supply-rail names for an MCU footprint (eng metadata subset — the
// only eng key coverify consumes). Shared with powerprofile via
// firmware/eng_rails.* (embedded starter-library table + instance overrides,
// metadata.resolve_eng precedence).
std::vector<std::string> eng_supply_rail_names(const nlohmann::json& fp) {
    std::vector<std::string> out;
    for (const auto& r : eng_supply_rails(fp)) {
        if (r["name"].is_string()) out.push_back(r["name"].get<std::string>());
    }
    return out;
}

nlohmann::json decoupling_checks(const nlohmann::json& board,
                                 const std::vector<nlohmann::json>& mcus) {
    nlohmann::json out = nlohmann::json::array();
    std::set<std::string> caps_nets;
    if (board.contains("footprints") && board["footprints"].is_array()) {
        for (const auto& fp : board["footprints"]) {
            if (fp.is_object() && is_capacitor(fp)) {
                auto ns = fp_nets(fp);
                caps_nets.insert(ns.begin(), ns.end());
            }
        }
    }
    std::set<std::string> board_nets;
    if (board.contains("nets") && board["nets"].is_array()) {
        for (const auto& n : board["nets"]) {
            if (n.is_object() && n.value("name", std::string()).size())
                board_nets.insert(n["name"].get<std::string>());
        }
    }

    for (const auto& fp : mcus) {
        std::string ref = fp.value("ref", std::string("?"));
        std::set<std::string> rails;
        for (const auto& nm : eng_supply_rail_names(fp)) rails.insert(nm);
        for (const auto& net : fp_nets(fp)) {
            if (is_power_net(net)) rails.insert(net);
        }
        for (const auto& rail : rails) {
            std::string up = rail;
            std::transform(up.begin(), up.end(), up.begin(),
                           [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
            if (kGroundNames.count(up)) continue;
            if (!board_nets.count(rail)) continue;  // metadata rail with no board net
            if (!caps_nets.count(rail)) {
                out.push_back(violation(
                    "fw_missing_decouple", "warning", rail, {ref},
                    "MCU " + ref + " supply rail " + rail +
                        " has no decoupling capacitor on the board (checked all capacitor "
                        "footprints' pads)",
                    "place a 100nF (plus bulk) capacitor between " + rail + " and GND close to " +
                        ref + "'s supply pins"));
            }
        }
    }
    return out;
}

std::string join(const std::vector<std::string>& v, const std::string& sep) {
    std::string out;
    for (size_t i = 0; i < v.size(); ++i) {
        if (i) out += sep;
        out += v[i];
    }
    return out;
}

nlohmann::json static_checks(const nlohmann::json& board,
                             const std::map<std::string, nlohmann::json>& fw_pins,
                             const nlohmann::json& sch, const std::string& mcu_family_in) {
    nlohmann::json out = nlohmann::json::array();
    std::string family = mcu_family_in;
    if (family.empty()) {
        if (auto fam = select_mcu_family(board)) family = *fam;
    }
    std::map<std::string, nlohmann::json> model;
    for (const auto& rec : pin_model(board, family)) model[rec["token"].get<std::string>()] = rec;
    std::vector<nlohmann::json> mcus = mcu_footprints(board);
    std::set<std::string> mcu_nets;
    for (const auto& fp : mcus) {
        auto ns = fp_nets(fp);
        mcu_nets.insert(ns.begin(), ns.end());
    }
    std::set<std::string> mcu_refs = mcu_sch_refs(sch);
    for (const auto& fp : mcus) mcu_refs.insert(fp.value("ref", std::string()));

    for (const auto& [token, use] : fw_pins) {
        auto it = model.find(token);
        if (it == model.end()) {
            out.push_back(violation(
                "fw_pin_unknown_net", "error", nullptr, {"PIN_" + token},
                "firmware references PIN_" + token +
                    " but no board net produces that pin-map token (power rails are never "
                    "emitted)",
                "add/rename a net matching PIN_" + token +
                    " on the board, or drop the firmware reference; regenerate pinmap.h"));
            continue;
        }
        const nlohmann::json& rec = it->second;
        std::string net = rec["net"].get<std::string>();
        if (!mcus.empty() && !mcu_nets.count(net)) {
            std::vector<std::string> refs;
            for (const auto& fp : mcus) refs.push_back(fp.value("ref", std::string("?")));
            std::sort(refs.begin(), refs.end());
            out.push_back(violation(
                "fw_pin_not_on_mcu", "warning", net, {"PIN_" + token},
                "net " + net + " exists but is not connected to any pad of the MCU footprint (" +
                    join(refs, ", ") + ")",
                "wire net " + net + " to the intended MCU pin, or stop referencing PIN_" + token +
                    " in firmware"));
        }
        long reads = use.value("reads", 0L), writes = use.value("writes", 0L);
        if (rec.value("strap", false) && (writes || reads)) {
            out.push_back(violation(
                "fw_strap_repurposed", "error", net, {"PIN_" + token},
                "boot-strap/reset pin " + net + " (" + (family.empty() ? "unknown family" : family) +
                    ") is used as a general GPIO by firmware (reads=" + std::to_string(reads) +
                    ", writes=" + std::to_string(writes) +
                    ") — its level at reset selects the boot mode",
                "move the signal to a free GPIO; keep " + net +
                    " on its strap resistor/reset circuit only"));
        }
        if (writes) {
            if (rec.value("input_only", false)) {
                out.push_back(violation(
                    "fw_drive_input_only", "error", net, {"PIN_" + token},
                    "firmware drives PIN_" + token + " but " + net +
                        " is an input-only pin on " + family + " (no output driver exists)",
                    "move the output to a pin with an output driver; input-only pins can only "
                    "sample"));
            }
            std::vector<std::string> drivers = external_strong_drivers(sch, net, mcu_refs);
            if (!drivers.empty()) {
                std::vector<std::string> pins = {"PIN_" + token};
                pins.insert(pins.end(), drivers.begin(), drivers.end());
                out.push_back(violation(
                    "fw_direction_clash", "error", net, pins,
                    "firmware drives net " + net + " which already has push-pull driver(s) " +
                        join(drivers, ", ") + " — two drivers fighting (output-output short)",
                    "configure PIN_" + token + " as input (the peripheral drives " + net +
                        "), or rewire the peripheral pin"));
            }
        }
    }

    nlohmann::json dec = decoupling_checks(board, mcus);
    for (const auto& v : dec) out.push_back(v);
    sort_findings(out);
    return out;
}

// ── dynamic checks (trace vs netlist expectations) ──────────────────────────

std::vector<std::string> trace_driven_pins(const nlohmann::json& trace) {
    std::set<std::string> pins;
    const nlohmann::json* events = nullptr;
    // Python: trace.get("events", []) or trace.get("gpio_events", []) — an EMPTY
    // events list is falsy and falls through to gpio_events.
    if (trace.contains("events") && trace["events"].is_array() && !trace["events"].empty())
        events = &trace["events"];
    else if (trace.contains("gpio_events") && trace["gpio_events"].is_array())
        events = &trace["gpio_events"];
    if (events) {
        for (const auto& ev : *events) {
            if (ev.is_object() && ev.value("type", std::string("gpio")) == "gpio" &&
                ev.contains("pin") && ev["pin"].is_string() && !ev["pin"].get<std::string>().empty())
                pins.insert(ev["pin"].get<std::string>());
        }
    }
    return {pins.begin(), pins.end()};
}

nlohmann::json dynamic_checks(const nlohmann::json& board, const nlohmann::json& trace,
                              const nlohmann::json& sch, const std::string& mcu_family_in) {
    nlohmann::json out = nlohmann::json::array();
    std::string family = mcu_family_in;
    if (family.empty()) {
        if (auto fam = select_mcu_family(board)) family = *fam;
    }
    std::vector<std::string> nets;
    if (board.contains("nets") && board["nets"].is_array()) {
        for (const auto& n : board["nets"]) {
            if (n.is_object() && n.value("name", std::string()).size())
                nets.push_back(n["name"].get<std::string>());
        }
    }
    std::map<int, std::string> id_to_net;
    for (const auto& n : nets) {
        if (auto pid = resolve_pin(n, family))
            id_to_net.emplace(*pid, n);  // first net wins on a collision
    }
    std::vector<nlohmann::json> mcus = mcu_footprints(board);
    std::set<std::string> mcu_refs = mcu_sch_refs(sch);
    for (const auto& fp : mcus) mcu_refs.insert(fp.value("ref", std::string()));

    for (const auto& pin : trace_driven_pins(trace)) {
        std::optional<std::string> net;
        if (std::find(nets.begin(), nets.end(), pin) != nets.end()) net = pin;
        if (!net) {
            if (auto pid = resolve_pin(pin, family)) {
                auto it = id_to_net.find(*pid);
                if (it != id_to_net.end()) net = it->second;
            }
        }
        if (!net) {
            out.push_back(violation(
                "fw_trace_unmapped_pin", "warning", nullptr, {pin},
                "emulation observed activity on pin " + pin +
                    " but no board net corresponds to it — the firmware toggles a pin the board "
                    "leaves unconnected",
                "route pin " + pin + " to its intended net, or remove the firmware output"));
            continue;
        }
        std::vector<std::string> problems;
        std::vector<std::string> pins_involved = {pin};
        if (is_strap_net(*net, family)) problems.push_back("it is a boot-strap/reset pin");
        std::vector<std::string> drivers = external_strong_drivers(sch, *net, mcu_refs);
        if (!drivers.empty()) {
            problems.push_back("push-pull driver(s) " + join(drivers, ", ") + " also drive it");
            pins_involved.insert(pins_involved.end(), drivers.begin(), drivers.end());
        }
        if (is_input_only_pin(*net, family)) problems.push_back("the net expects an input-only pin");
        if (!problems.empty()) {
            out.push_back(violation(
                "fw_trace_drive_conflict", "error", *net, pins_involved,
                "emulation observed the firmware driving " + pin + " (net " + *net + ") but " +
                    join(problems, "; "),
                "stop driving " + pin + " in firmware, or rewire net " + *net +
                    " so it has a single driver"));
        }
    }
    sort_findings(out);
    return out;
}

}  // namespace

std::map<std::string, nlohmann::json> scan_firmware_pins(
    const std::map<std::string, std::string>& sources) {
    std::map<std::string, nlohmann::json> out;
    for (const auto& [fname, text] : sources) {
        std::istringstream in(text);
        std::string line;
        while (std::getline(in, line)) {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            std::vector<std::string> tokens;
            for (auto it = std::sregex_iterator(line.begin(), line.end(), kPinRefRe);
                 it != std::sregex_iterator(); ++it) {
                tokens.push_back((*it)[1].str());
            }
            if (tokens.empty()) continue;
            bool is_write = std::regex_search(line, kWriteRe);
            bool is_read = std::regex_search(line, kReadRe);
            std::set<std::string> mode_out, mode_in;
            for (auto it = std::sregex_iterator(line.begin(), line.end(), kModeOutRe);
                 it != std::sregex_iterator(); ++it) mode_out.insert((*it)[1].str());
            for (auto it = std::sregex_iterator(line.begin(), line.end(), kModeInRe);
                 it != std::sregex_iterator(); ++it) mode_in.insert((*it)[1].str());
            for (const auto& tok : tokens) {
                nlohmann::json& rec = out[tok];
                if (rec.is_null()) rec = {{"reads", 0}, {"writes", 0}, {"refs", 0},
                                          {"files", nlohmann::json::array()}};
                rec["refs"] = rec["refs"].get<long>() + 1;
                if (is_write || mode_out.count(tok)) rec["writes"] = rec["writes"].get<long>() + 1;
                if (is_read || mode_in.count(tok)) rec["reads"] = rec["reads"].get<long>() + 1;
                auto& files = rec["files"];
                if (std::find(files.begin(), files.end(), fname) == files.end()) files.push_back(fname);
            }
        }
    }
    return out;
}

std::map<std::string, std::string> project_sources(const Project& project) {
    std::map<std::string, std::string> out;
    auto abs = project.abs_sources();
    for (size_t i = 0; i < project.src_paths.size() && i < abs.size(); ++i) {
        std::ifstream in(abs[i], std::ios::binary);
        if (!in) continue;
        std::stringstream ss;
        ss << in.rdbuf();
        out[project.src_paths[i]] = ss.str();
    }
    return out;
}

nlohmann::json load_trace(const std::string& trace_path) {
    try {
        std::ifstream in(trace_path);
        if (!in) return nlohmann::json();
        std::stringstream ss;
        ss << in.rdbuf();
        nlohmann::json data = nlohmann::json::parse(ss.str(), nullptr, false);
        return data.is_object() ? data : nlohmann::json();
    } catch (...) {
        return nlohmann::json();
    }
}

nlohmann::json verify_board(const nlohmann::json& board,
                            const std::map<std::string, nlohmann::json>& fw_pins,
                            const nlohmann::json& sch, const std::string& mcu_family,
                            const nlohmann::json& trace) {
    nlohmann::json out = static_checks(board, fw_pins, sch, mcu_family);
    if (trace.is_object()) {
        nlohmann::json dyn = dynamic_checks(board, trace, sch, mcu_family);
        for (const auto& v : dyn) out.push_back(v);
    }
    sort_findings(out);
    return out;
}

nlohmann::json summarize(const nlohmann::json& findings) {
    long errors = 0, warnings = 0;
    std::set<std::string> rules;
    if (findings.is_array()) {
        for (const auto& f : findings) {
            std::string sev = f.value("severity", std::string());
            if (sev == "error") ++errors;
            else if (sev == "warning") ++warnings;
            rules.insert(f.value("rule", std::string()));
        }
    }
    return {
        {"n", findings.is_array() ? findings.size() : 0},
        {"errors", errors},
        {"warnings", warnings},
        {"rules", std::vector<std::string>(rules.begin(), rules.end())},
    };
}

}  // namespace chudware::firmware
