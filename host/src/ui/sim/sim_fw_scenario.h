#pragma once
// Thin kit editor for the executable aicad.fwscenario/1 object. Only explicit edits
// change keys; raw JSON retains all native timing/electrical and unknown metadata.
#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <limits>
#include <sstream>
#include <string>
#include <set>
#include <utility>
#include <vector>
#include <nlohmann/json.hpp>
#include "ui/dragdrop.h"
#include "ui/file_dialog.h"
#include "ui/widgets/kit.h"

namespace ui::sim {
inline constexpr const char* kFwTrackDragPayload = "CHUDWARE_FW_TRACK";
inline constexpr const char* kFwTrackOrderPayload = "CHUDWARE_FW_ORDER";
struct FwScenarioEditor {
    std::vector<char> raw = std::vector<char>(4096, '\0');
    std::string path, error, validationError;
    bool rawValid = true, draftBlocked = false, initialized = false;
    nlohmann::json synchronized;
};

inline bool fwScenarioNumber(const nlohmann::json& value) {
    return value.is_number() && std::isfinite(value.get<double>());
}
inline bool fwScenarioBytes(const nlohmann::json& value) {
    if (!value.is_array() || value.empty()) return false;
    for (const auto& byte : value)
        if (!byte.is_number_integer() || byte < 0 || byte > 255) return false;
    return true;
}
inline bool fwScenarioHex(const nlohmann::json& value, bool empty = false, size_t maxBytes = 0) {
    if (!value.is_string()) return false;
    const auto& text = value.get_ref<const std::string&>();
    return (empty || !text.empty()) && text.size() % 2 == 0 &&
           (!maxBytes || text.size() <= maxBytes * 2) &&
           text.find_first_not_of("0123456789abcdefABCDEF") == std::string::npos;
}
inline bool fwScenarioRegisters(const nlohmann::json& value) {
    if (!value.is_object() || value.empty()) return false;
    for (const auto& item : value.items()) {
        try {
            size_t used = 0;
            const int address = std::stoi(item.key(), &used, item.key().rfind("0x", 0) == 0 ? 16 : 10);
            if (used != item.key().size() || address < 0 || address > 255 ||
                !item.value().is_number_integer() || item.value() < 0 || item.value() > 255) return false;
        } catch (...) { return false; }
    }
    return true;
}
inline bool fwScenarioPin(const nlohmann::json& value) {
    if (!value.is_string()) return false;
    const auto& pin = value.get_ref<const std::string&>();
    if (pin.size() < 3 || pin.size() > 4 || pin[0] != 'P' || pin[1] < 'A' || pin[1] > 'K') return false;
    if (pin.find_first_not_of("0123456789", 2) != std::string::npos) return false;
    return std::stoi(pin.substr(2)) <= 15;
}
inline bool validateFwScenarioLinks(const nlohmann::json& scenario, std::string& error) {
    using json = nlohmann::json;
    auto text = [](const json& object, const char* key) -> std::string {
        return object.contains(key) && object[key].is_string() ? object[key].get<std::string>() : "";
    };
    auto number = [](const json& object, const char* key, bool positive = false) {
        return object.contains(key) && fwScenarioNumber(object[key]) &&
               (positive ? object[key].get<double>() > 0 : object[key].get<double>() >= 0);
    };
    auto fail = [&](const char* reason) { error = reason; return false; };
    if (!scenario.contains("bus_links")) {
        for (const auto& track : scenario["tracks"])
            if (track.is_object() && track.contains("link_id")) return fail("Track link_id requires bus_links.");
        return true;
    }
    if (!scenario["bus_links"].is_array()) return fail("bus_links must be an array.");
    std::set<std::string> ids, buses;
    for (const auto& link : scenario["bus_links"]) {
        if (!link.is_object()) return fail("bus_links entries must be objects.");
        const auto id = text(link, "id"), bus = text(link, "bus"), protocol = text(link, "protocol");
        if (id.empty() || !ids.insert(id).second || bus.empty() || !buses.insert(bus).second ||
            bus[0] < 'a' || bus[0] > 'z' || bus.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789") != std::string::npos)
            return fail("Links need unique id/bus and a monitor-safe peripheral bus name.");
        for (const char* key : {"limitations", "sources"})
            if (link.contains(key) && !link[key].is_array()) return fail("Link sources/limitations must be arrays.");
        std::set<std::string> expected;
        if (protocol == "i2c") expected = {"scl", "sda"};
        else if (protocol == "uart") expected = {"tx", "rx"};
        else if (protocol == "spi") expected = {"sck", "mosi", "miso", "cs"};
        else if (protocol == "can") expected = {"can_diff"};
        else return fail("Link protocol must be i2c, uart, spi or can.");
        if (!number(link, "controller_clock_hz", true)) return fail("Declare the actual finite positive controller_clock_hz.");
        if (!link.contains("peer") || !link["peer"].is_object()) return fail("Link needs an explicit peer object.");
        const auto& peer = link["peer"];
        for (const char* key : {"response_delay_s", "clock_stretch_s", "clock_to_output_s", "setup_s", "hold_s"})
            if (peer.contains(key) && !number(peer, key)) return fail("Peer delays must be finite nonnegative seconds.");
        if (protocol == "uart" && (!number(peer, "baud_hz", true) || text(peer, "electrical_variant") != "ttl"))
            return fail("UART link needs peer baud_hz and explicit electrical_variant ttl.");
        if (protocol == "can" && !number(peer, "bitrate_hz", true)) return fail("CAN link needs peer bitrate_hz.");
        if (protocol == "spi" && (text(peer, "controller_mode") == "slave" || text(peer, "variant") == "i2s" || text(peer, "variant") == "one_wire"))
            return fail("Native SPI slave, I2S and one-wire variants are unsupported.");
        if (protocol == "spi" && peer.contains("cs_gpio")) {
            const auto gpio = text(peer, "cs_gpio");
            if (gpio.size() != 9 || gpio.rfind("gpioPort", 0) != 0 || gpio[8] < 'A' || gpio[8] > 'K' ||
                !peer.contains("cs_pin") || !peer["cs_pin"].is_number_integer() || peer["cs_pin"] < 0 || peer["cs_pin"] > 15)
                return fail("SPI peer CS needs gpioPortA through gpioPortK and cs_pin 0 through 15.");
        }
        if (!link.contains("electrical") || !link["electrical"].is_object() ||
            !link["electrical"].contains("signals") || !link["electrical"]["signals"].is_array())
            return fail("Links need complete electrical.signals; omit links for functional mode.");
        std::set<std::string> declared;
        for (const auto& signal : link["electrical"]["signals"]) {
            if (!signal.is_object()) return fail("Signals must be objects.");
            const auto name = text(signal, "name"), model = text(signal, "model");
            if (!expected.count(name) || !declared.insert(name).second ||
                text(signal, "mode") != (protocol == "can" ? "differential" : "single_ended"))
                return fail("Signals need unique protocol names and matching modes.");
            if (!signal.contains("endpoints") || !signal["endpoints"].is_array() || signal["endpoints"].size() < 2)
                return fail("Signals need mcu and peer endpoints.");
            std::set<std::string> endpoints, locations;
            for (const auto& endpoint : signal["endpoints"]) {
                if (!endpoint.is_object() || text(endpoint, "id").empty() || !endpoints.insert(text(endpoint, "id")).second ||
                    text(endpoint, "at").empty()) return fail("Endpoints need unique id and explicit at.");
                locations.insert(text(endpoint, "at"));
                for (const char* key : {"input_capacitance_f", "rise_s", "fall_s", "tx_delay_s", "rx_delay_s", "v_low_v", "v_high_v"})
                    if (!number(endpoint, key)) return fail("Declare every finite nonnegative endpoint capacitance, transition, delay and voltage.");
                for (const char* key : {"r_low_ohm", "r_high_ohm"})
                    if (!number(endpoint, key, true)) return fail("Endpoint driver resistance must be explicit finite positive ohms.");
                if (protocol == "can") {
                    const bool external = text(endpoint, "role") == "external_driver" && text(endpoint, "id") != "mcu" && text(endpoint, "id") != "peer";
                    if ((text(endpoint, "drive") != "can_dominant" && !(external && text(endpoint, "drive") == "push_pull")) ||
                        !number(endpoint, "recessive_max_v") || !number(endpoint, "dominant_min_v", true) ||
                        endpoint["recessive_max_v"] >= endpoint["dominant_min_v"])
                        return fail("CAN endpoints need supported drive and ordered differential thresholds.");
                } else if (text(endpoint, "drive") != (protocol == "i2c" ? "open_drain" : "push_pull") ||
                           !number(endpoint, "vil_v") || !number(endpoint, "vih_v", true) || endpoint["vil_v"] >= endpoint["vih_v"])
                    return fail("Endpoints need matching drive and ordered vil_v/vih_v.");
            }
            if (!endpoints.count("mcu") || !endpoints.count("peer")) return fail("Signals need named mcu and peer endpoints.");
            if (signal.contains("segments") && !signal["segments"].is_array()) return fail("segments must be an array.");
            if (model == "lumped_rc") {
                if (!number(signal, "series_r_ohm") || !number(signal, "wire_capacitance_f") ||
                    (signal.contains("segments") && !signal["segments"].empty()))
                    return fail("lumped_rc needs explicit series_r_ohm/wire_capacitance_f, without distributed segments.");
            } else if (model == "lossless_tline") {
                if ((signal.contains("wire_capacitance_f") && (!number(signal, "wire_capacitance_f") || signal["wire_capacitance_f"] != 0)) ||
                    !signal.contains("segments") || !signal["segments"].is_array() || signal["segments"].empty())
                    return fail("lossless_tline needs segments, without double-counted wire capacitance.");
                for (const auto& segment : signal["segments"]) {
                    if (!segment.is_object() || text(segment, "from").empty() || text(segment, "to").empty() ||
                        text(segment, "from") == text(segment, "to")) return fail("Segments need distinct from/to locations.");
                    // Native ECAD impedance resolution, not a nominal GUI fallback, owns omitted values.
                    const bool derived = segment.contains("ecad_source") && segment["ecad_source"].is_object() &&
                        segment["ecad_source"].contains("board") && segment["ecad_source"].contains("track_or_net");
                    if ((segment.contains("z0_ohm") ? !number(segment, "z0_ohm", true) : !derived) ||
                        (segment.contains("propagation_s") ? !number(segment, "propagation_s", true) : !derived))
                        return fail("Segments need positive impedance/delay or an explicit native ECAD source.");
                    locations.insert(text(segment, "from")); locations.insert(text(segment, "to"));
                }
            } else return fail("Signal model must be lumped_rc or lossless_tline.");
            for (const char* key : {"bias", "terminations"}) if (signal.contains(key)) {
                if (!signal[key].is_array()) return fail("Bias/terminations must be arrays.");
                for (const auto& element : signal[key]) {
                    if (!element.is_object() || !number(element, "resistance_ohm", true) || !locations.count(text(element, "at")) ||
                        (element.contains("other_at") && !locations.count(text(element, "other_at"))) ||
                        (element.contains("voltage_v") && !number(element, "voltage_v")))
                        return fail("Bias/termination needs known locations and positive resistance, with nonnegative voltage.");
                    if (std::strcmp(key, "bias") == 0) {
                        const auto to = text(element, "to");
                        if ((to != "ground" && to != "rail" && to != "differential") || (to != "ground" && !number(element, "voltage_v")))
                            return fail("Bias needs ground, rail or differential target and explicit rail/differential voltage.");
                    }
                }
            }
        }
        if (declared != expected) return fail("Declare every protocol signal; partial electrical fallback is not supported.");
    }
    for (const auto& track : scenario["tracks"]) if (track.is_object() && track.contains("link_id")) {
        if (!ids.count(text(track, "link_id"))) return fail("Unknown track link_id.");
        for (const auto& link : scenario["bus_links"]) if (link["id"] == track["link_id"]) {
            const auto kind = text(track, "kind"), protocol = text(link, "protocol");
            if ((track.contains("bus") && track["bus"] != link["bus"]) ||
                (kind.rfind("i2c_", 0) == 0 && protocol != "i2c") || (kind.rfind("spi_", 0) == 0 && protocol != "spi") ||
                (kind == "uart_rx" && protocol != "uart") || (kind == "can_frames" && protocol != "can"))
                return fail("Track bus/protocol conflicts with link_id.");
        }
    }
    return true;
}
inline bool fwScenarioFixtureReference(const nlohmann::json& value) {
    if (!value.is_string()) return false;
    const auto& text = value.get_ref<const std::string&>();
    if (text.rfind("fixture:", 0) == 0 && text.size() > 8 &&
        text.find_first_of(" \t\r\n", 8) == std::string::npos) return true;
    // Native load_scenario also accepts the existing fixture names without a prefix.
    return text == "i2c_temp_tmp103" || text == "spi_imu_stub" ||
           text == "quadrature_encoder" || text == "adc_thermistor" || text == "user_button";
}
// Structural preflight follows core/firmware/scenario.cpp. Native fw_sim_run still
// validates electrical networks, controller capabilities and backend limitations.
inline bool validateFwScenario(const nlohmann::json& scenario, std::string& error) {
    error.clear();
    auto fail = [&](const std::string& reason) { error = reason; return false; };
    if (scenario.is_null()) return true; // no scenario is a supported native run
    if (fwScenarioFixtureReference(scenario)) return true; // native resolves fixture names
    if (!scenario.is_object() || !scenario.contains("schema") ||
        scenario["schema"] != "aicad.fwscenario/1") return fail("Schema must be aicad.fwscenario/1.");
    if (!scenario.contains("tracks") || !scenario["tracks"].is_array() || scenario["tracks"].empty())
        return fail("Scenario needs at least one stimulus track.");
    for (size_t index = 0; index < scenario["tracks"].size(); ++index) {
        const auto& tr = scenario["tracks"][index];
        const std::string prefix = "Track " + std::to_string(index + 1) + ": ";
        auto bad = [&](const char* reason) { return fail(prefix + reason); };
        if (!tr.is_object() || !tr.contains("kind") || !tr["kind"].is_string()) return bad("needs a native kind.");
        const auto& kind = tr["kind"].get_ref<const std::string&>();
        auto hasNumber = [&](const char* key) { return tr.contains(key) && fwScenarioNumber(tr[key]); };
        if (kind == "gpio_edges" || kind == "button") {
            if (!tr.contains("pin") || !fwScenarioPin(tr["pin"])) return bad("pin must be PA0 through PK15.");
        } else if (kind == "quadrature") {
            if (!tr.contains("pin_a") || !fwScenarioPin(tr["pin_a"]) ||
                !tr.contains("pin_b") || !fwScenarioPin(tr["pin_b"]) ||
                !tr.contains("steps") || !tr["steps"].is_number_integer() || tr["steps"] < 1 ||
                !hasNumber("step_ms") || tr["step_ms"] <= 0 ||
                (tr.contains("direction") && tr["direction"] != "cw" && tr["direction"] != "ccw"))
                return bad("needs valid pins, positive steps/step_ms and cw or ccw direction.");
        } else if (kind == "i2c_sensor" || kind == "i2c_registers" || kind == "i2c_register_stream") {
            if (!tr.contains("address") || !tr["address"].is_number_integer() || tr["address"] < 0 || tr["address"] > 127)
                return bad("I2C address must be an integer from 0 to 127.");
            if (kind == "i2c_sensor" && (!tr.contains("model") || tr["model"] != "tmp103" || !hasNumber("temperature_c")))
                return bad("TMP103 needs model tmp103 and temperature_c.");
            if (tr.contains("registers") && !fwScenarioRegisters(tr["registers"])) return bad("registers must map byte addresses to bytes.");
            if (kind != "i2c_sensor" && (!tr.contains("samples") || tr["samples"].is_null()) &&
                !(tr.contains("response") && fwScenarioBytes(tr["response"])) &&
                !(tr.contains("registers") && fwScenarioRegisters(tr["registers"])))
                return bad("needs response bytes, registers or time-ordered samples.");
        } else if (kind == "spi_stub" || kind == "spi_peer") {
            if (!tr.contains("bus") || !tr["bus"].is_string() || tr["bus"].empty()) return bad("needs a platform SPI bus.");
            if (kind == "spi_peer" && !tr.contains("response_hex") && !tr.contains("registers") && !tr.contains("link_id"))
                return bad("SPI peer needs response_hex, registers or link_id.");
            if (tr.contains("registers") && !fwScenarioRegisters(tr["registers"])) return bad("registers must map byte addresses to bytes.");
        } else if (kind == "adc_waveform") {
            if (!tr.contains("waveform") || !tr["waveform"].is_object()) return bad("needs waveform.");
            const auto& wf = tr["waveform"];
            if (!wf.contains("type") || !wf["type"].is_string()) return bad("waveform needs a type.");
            if (wf["type"] == "samples") {
                if (!wf.contains("values") || !wf["values"].is_array() || wf["values"].empty()) return bad("waveform needs sample values.");
                for (const auto& v : wf["values"]) if (!fwScenarioNumber(v)) return bad("waveform values must be numeric.");
            } else if (wf["type"] == "sine" || wf["type"] == "ramp") {
                for (const char* key : {"min", "max", "period_ms"})
                    if (!wf.contains(key) || !fwScenarioNumber(wf[key])) return bad("waveform needs min, max and period_ms.");
            } else return bad("unsupported waveform type.");
        } else if (kind != "uart_rx" && kind != "can_frames") return bad("unknown native track kind.");
        if (tr.contains("response_hex") && !fwScenarioHex(tr["response_hex"])) return bad("response_hex needs complete hex bytes.");
        if (tr.contains("register_start") && (!tr["register_start"].is_number_integer() || tr["register_start"] < 0 || tr["register_start"] > 255))
            return bad("register_start must be a byte address.");
        if (kind == "uart_rx" && tr.contains("baud_hz") && (!hasNumber("baud_hz") || tr["baud_hz"] <= 0)) return bad("baud_hz must be finite and positive.");
        if (tr.contains("cs_gpio")) {
            if (!tr["cs_gpio"].is_string()) return bad("SPI CS needs gpioPortA through gpioPortK.");
            const auto& gpio = tr["cs_gpio"].get_ref<const std::string&>();
            if (gpio.size() != 9 || gpio.rfind("gpioPort", 0) != 0 || gpio[8] < 'A' || gpio[8] > 'K' ||
                !tr.contains("cs_pin") || !tr["cs_pin"].is_number_integer() || tr["cs_pin"] < 0 || tr["cs_pin"] > 15)
                return bad("SPI CS needs gpioPortA through gpioPortK and cs_pin 0 through 15.");
        }
        const char* requiredList = kind == "button" ? "presses" :
            (kind == "gpio_edges" || kind == "uart_rx" || kind == "can_frames") ? "events" : nullptr;
        if (requiredList && (!tr.contains(requiredList) || !tr[requiredList].is_array() || tr[requiredList].empty()))
            return bad("needs a nonempty event list.");
        for (const char* key : {"events", "presses", "samples", "drive_events"}) {
            if (!tr.contains(key) || (tr[key].is_null() && std::strcmp(key, "drive_events") != 0)) continue;
            const bool samples = std::strcmp(key, "samples") == 0 &&
                (kind == "i2c_registers" || kind == "i2c_register_stream");
            const bool drives = std::strcmp(key, "drive_events") == 0;
            if (!samples && !drives && (!requiredList || std::strcmp(key, requiredList) != 0)) {
                if (tr[key].is_array()) for (const auto& event : tr[key])
                    if (event.is_object() && event.contains("t_ms") && (!fwScenarioNumber(event["t_ms"]) || event["t_ms"] < 0))
                        return bad("stimulus t_ms must be finite nonnegative.");
                continue;
            }
            if (!tr[key].is_array() || (!drives && tr[key].empty())) return bad("event/sample lists must be nonempty arrays.");
            double previous = -1;
            for (const auto& event : tr[key]) {
                if (!event.is_object() || !event.contains("t_ms") || !fwScenarioNumber(event["t_ms"]) || event["t_ms"] < 0)
                    return bad("events need finite nonnegative t_ms.");
                const double time = event["t_ms"].get<double>();
                if (std::strcmp(key, "samples") == 0) {
                    if (time < previous || (!(event.contains("response") && fwScenarioBytes(event["response"])) &&
                        !(event.contains("registers") && fwScenarioRegisters(event["registers"]))))
                        return bad("samples need ordered times and response bytes or registers.");
                    previous = time;
                } else if (std::strcmp(key, "drive_events") == 0) {
                    if (kind != "can_frames" || !event.contains("endpoint") || !event["endpoint"].is_string() ||
                        !event.contains("state") || !event["state"].is_number_integer() || event["state"] < -1 || event["state"] > 1)
                        return bad("CAN drive events need endpoint and state -1, 0 or 1.");
                } else if (kind == "gpio_edges") {
                    if (!event.contains("level") || !event["level"].is_number_integer() || (event["level"] != 0 && event["level"] != 1))
                        return bad("GPIO level must be 0 or 1.");
                } else if (kind == "uart_rx") {
                    if (!event.contains("text") || !event["text"].is_string() || event["text"].empty()) return bad("UART events need nonempty text.");
                } else if (kind == "can_frames") {
                    if (!event.contains("id") || !event["id"].is_number_integer() || event["id"] < 0 ||
                        (event.contains("extended") && !event["extended"].is_boolean()) ||
                        event["id"] > (event.value("extended", false) ? 0x1fffffff : 0x7ff) ||
                        !event.contains("payload_hex") || !fwScenarioHex(event["payload_hex"], true, 8))
                        return bad("CAN events need a valid classical frame id and 0 to 8 hex bytes.");
                }
            }
        }
    }
    return validateFwScenarioLinks(scenario, error);
}
inline bool parseFwScenarioText(const std::string& text, nlohmann::json& output, std::string& error) {
    const auto first = text.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) { output = nullptr; error.clear(); return true; }
    const auto trimmed = text.substr(first, text.find_last_not_of(" \t\r\n") - first + 1);
    auto candidate = nlohmann::json::parse(trimmed, nullptr, false);
    if (!candidate.is_object()) {
        candidate = trimmed;
        if (!fwScenarioFixtureReference(candidate)) {
            error = "Scenario must contain a native JSON object or fixture reference. Draft preserved.";
            return false;
        }
    }
    if (!validateFwScenario(candidate, error)) return false;
    output = std::move(candidate);
    return true;
}
inline void fwScenarioSetRaw(std::vector<char>& raw, const std::string& text) {
    raw.resize(std::max(size_t(4096), text.size() + 1));
    std::memcpy(raw.data(), text.c_str(), text.size() + 1);
}
inline void syncFwScenarioEditor(FwScenarioEditor& editor, const nlohmann::json& canonical,
                                 std::vector<char>* externalRaw = nullptr) {
    auto& raw = externalRaw ? *externalRaw : editor.raw;
    fwScenarioSetRaw(raw, canonical.is_null() ? "" : canonical.is_string()
        ? canonical.get_ref<const std::string&>() : canonical.dump(2));
    editor.synchronized = canonical;
    editor.initialized = true;
    editor.draftBlocked = fwScenarioFixtureReference(canonical);
    editor.rawValid = editor.draftBlocked || validateFwScenario(canonical, editor.validationError);
    if (editor.draftBlocked) editor.validationError.clear();
}
inline bool loadFwScenarioFile(FwScenarioEditor& editor, nlohmann::json& canonical,
                               const std::string& path, std::vector<char>* externalRaw = nullptr) {
    std::ifstream file(path, std::ios::binary);
    if (!file) { editor.error = "Cannot open scenario file. Existing editor preserved."; return false; }
    std::ostringstream content;
    content << file.rdbuf();
    if (file.bad()) { editor.error = "Cannot read scenario file. Existing editor preserved."; return false; }
    nlohmann::json candidate;
    const auto text = content.str();
    if (!parseFwScenarioText(text, candidate, editor.error) || !candidate.is_object()) {
        if (editor.error.empty()) editor.error = "File must contain a native scenario object.";
        return false;
    }
    canonical = std::move(candidate);
    syncFwScenarioEditor(editor, canonical, externalRaw);
    fwScenarioSetRaw(externalRaw ? *externalRaw : editor.raw, text);
    editor.path = path;
    editor.error.clear();
    return true;
}
inline nlohmann::json fwScenarioTrackTemplate(const std::string& kind) {
    using json = nlohmann::json;
    if (kind == "gpio_edges") return {{"kind", kind}, {"pin", "PA0"}, {"events", json::array({{{"t_ms", 0.0}, {"level", 1}}})}};
    if (kind == "button") return {{"kind", kind}, {"pin", "PA0"}, {"presses", json::array({{{"t_ms", 0.0}, {"hold_ms", 50.0}}})}};
    if (kind == "quadrature") return {{"kind", kind}, {"pin_a", "PA0"}, {"pin_b", "PA1"}, {"steps", 1}, {"step_ms", 1.0}};
    if (kind == "uart_rx") return {{"kind", kind}, {"uart", "usart1"}, {"events", json::array({{{"t_ms", 0.0}, {"text", "A"}}})}};
    if (kind == "i2c_sensor") return {{"kind", kind}, {"model", "tmp103"}, {"bus", "i2c1"}, {"address", 72}, {"temperature_c", 25.0}};
    if (kind == "i2c_registers") return {{"kind", kind}, {"bus", "i2c1"}, {"address", 72}, {"response", json::array({0})}};
    if (kind == "i2c_register_stream") return {{"kind", kind}, {"bus", "i2c1"}, {"address", 72}, {"samples", json::array({{{"t_ms", 0.0}, {"response", json::array({0})}}})}};
    if (kind == "spi_stub") return {{"kind", kind}, {"bus", "spi1"}};
    if (kind == "spi_peer") return {{"kind", kind}, {"bus", "spi1"}, {"response_hex", "00"}};
    if (kind == "can_frames") return {{"kind", kind}, {"bus", "can1"}, {"events", json::array({{{"t_ms", 0.0}, {"id", 0}, {"payload_hex", ""}}})}};
    if (kind == "adc_waveform") return {{"kind", kind}, {"channel", 0}, {"waveform", {{"type", "samples"}, {"values", json::array({0})}}}};
    return nullptr;
}
inline bool addFwScenarioTrack(nlohmann::json& scenario, const std::string& kind, std::string& error) {
    auto track = fwScenarioTrackTemplate(kind);
    if (track.is_null()) { error = "Unknown native track kind."; return false; }
    if (scenario.is_null()) scenario = {{"schema", "aicad.fwscenario/1"}, {"tracks", nlohmann::json::array()}};
    if (!scenario.is_object() || !scenario.contains("tracks") || !scenario["tracks"].is_array()) {
        error = "Repair the scenario JSON before adding tracks."; return false;
    }
    scenario["tracks"].push_back(std::move(track));
    error.clear();
    return true;
}
inline bool moveFwScenarioTrack(nlohmann::json& scenario, size_t from, size_t to, std::string& error) {
    if (!scenario.is_object() || !scenario.contains("tracks") || !scenario["tracks"].is_array() ||
        from >= scenario["tracks"].size() || to >= scenario["tracks"].size()) {
        error = "Invalid track position."; return false;
    }
    if (from == to) return false;
    auto& tracks = scenario["tracks"];
    auto moved = std::move(tracks[from]);
    tracks.erase(tracks.begin() + from);
    tracks.insert(tracks.begin() + to, std::move(moved));
    error.clear();
    return true;
}
inline int fwScenarioResizeInput(ImGuiInputTextCallbackData* data) {
    if (data->EventFlag == ImGuiInputTextFlags_CallbackResize) {
        auto& raw = *static_cast<std::vector<char>*>(data->UserData);
        raw.resize(static_cast<size_t>(data->BufSize));
        data->Buf = raw.data();
    }
    return 0;
}
// Unit of a scenario number, read from its native key suffix (t_ms -> ms).
inline const char* fwScenarioFieldUnit(const char* key) {
    const std::string name(key);
    if (name.size() > 3 && name.compare(name.size() - 3, 3, "_ms") == 0) return "ms";
    if (name.size() > 3 && name.compare(name.size() - 3, 3, "_hz") == 0) return "Hz";
    if (name.size() > 4 && name.compare(name.size() - 4, 4, "_ohm") == 0) return "Ohm";
    if (name.size() > 2 && name.compare(name.size() - 2, 2, "_s") == 0) return "s";
    if (name.size() > 2 && name.compare(name.size() - 2, 2, "_f") == 0) return "F";
    if (name.size() > 2 && name.compare(name.size() - 2, 2, "_v") == 0) return "V";
    if (name.size() > 2 && name.compare(name.size() - 2, 2, "_c") == 0) return "\xc2\xb0" "C";
    return "";
}
// Display name of a stimulus kind (the palette and track titles use the same words).
inline const char* fwScenarioKindTitle(const std::string& kind) {
    static const std::pair<const char*, const char*> kTitles[] = {
        {"gpio_edges", "GPIO edges"}, {"uart_rx", "UART receive"},
        {"i2c_register_stream", "I2C register stream"}, {"spi_peer", "SPI peer"},
        {"can_frames", "CAN frames"}, {"button", "Button"}, {"quadrature", "Quadrature"},
        {"i2c_sensor", "I2C TMP103"}, {"i2c_registers", "I2C registers"},
        {"spi_stub", "SPI bus (no peer)"}, {"adc_waveform", "ADC waveform"}};
    for (const auto& t : kTitles)
        if (kind == t.first) return t.second;
    return nullptr;
}
// Field label for a native scenario key: plain words, unit suffix dropped (the unit is shown
// beside the value), bus acronyms upper-cased. The key itself stays the JSON identity.
inline std::string fwScenarioKeyLabel(const std::string& key) {
    static const std::pair<const char*, const char*> kKnown[] = {
        {"t_ms", "Time"}, {"hold_ms", "Hold time"}, {"kind", "Stimulus"}, {"schema", "Schema"},
        {"uart", "UART"}, {"bus", "Bus"}, {"pin", "Pin"}, {"level", "Level"},
        {"address", "Address"}, {"response", "Response bytes"}, {"response_hex", "Response (hex)"},
        {"payload_hex", "Payload (hex)"}, {"id", "ID"}, {"temperature_c", "Temperature"},
        {"baud_hz", "Baud rate"}, {"text", "Text"}, {"events", "Events"}, {"presses", "Presses"},
        {"samples", "Samples"}, {"model", "Model"}, {"tracks", "Tracks"}, {"links", "Electrical links"}};
    for (const auto& k : kKnown)
        if (key == k.first) return k.second;
    std::string base = key;
    for (const char* suffix : {"_ms", "_hz", "_ohm", "_s", "_f", "_v", "_c"}) {
        const std::size_t n = std::strlen(suffix);
        if (base.size() > n && base.compare(base.size() - n, n, suffix) == 0) {
            base.resize(base.size() - n);
            break;
        }
    }
    std::string out;
    std::size_t start = 0;
    while (start <= base.size()) {
        std::size_t end = base.find('_', start);
        if (end == std::string::npos) end = base.size();
        std::string word = base.substr(start, end - start);
        std::string upper = word;
        for (char& ch : upper) ch = (char)std::toupper((unsigned char)ch);
        if (upper == "GPIO" || upper == "UART" || upper == "I2C" || upper == "SPI" ||
            upper == "CAN" || upper == "ADC" || upper == "ID" || upper == "TX" || upper == "RX")
            word = upper;
        if (!word.empty()) {
            if (!out.empty()) out += ' ';
            out += word;
        }
        start = end + 1;
    }
    if (!out.empty()) out[0] = (char)std::toupper((unsigned char)out[0]);
    return out.empty() ? key : out;
}
inline bool drawFwScenarioValue(const char* id, const char* label, nlohmann::json& value, int depth = 0) {
    bool changed = false;
    ImGui::PushID(id);
    if (value.is_object() || value.is_array()) {
        if (depth < 24 && ImGui::TreeNodeEx("fields", ImGuiTreeNodeFlags_DefaultOpen, "%s (%zu)", label, value.size())) {
            if (value.is_object()) {
                kit::FieldGroup fields("fields_group");
                for (auto& item : value.items()) {
                    const std::string itemLabel = fwScenarioKeyLabel(item.key());
                    if (item.key() == "kind" || item.key() == "schema") {
                        if (item.value().is_string()) {
                            const std::string& raw = item.value().get_ref<const std::string&>();
                            const char* title = item.key() == "kind" ? fwScenarioKindTitle(raw) : nullptr;
                            kit::fieldReadout(item.key().c_str(), itemLabel.c_str(), title ? title : raw.c_str());
                        }
                    } else changed |= drawFwScenarioValue(item.key().c_str(), itemLabel.c_str(), item.value(), depth + 1);
                }
            } else {
                int remove = -1, moveFrom = -1, moveTo = -1;
                for (size_t i = 0; i < value.size(); ++i) {
                    ImGui::PushID(static_cast<int>(i));
                    bool removed = false;
                    const std::string title = std::string(label) + " " + std::to_string(i + 1);
                    if (kit::removableCard("entry", title.c_str(), nullptr, nullptr, "Remove entry", &removed)) {
                        changed |= drawFwScenarioValue("value", "Parameters", value[i], depth + 1);
                        if (kit::secondaryButton("Move earlier", i > 0, nullptr, ImVec2(-1, 0))) { moveFrom = static_cast<int>(i); moveTo = moveFrom - 1; }
                        if (kit::secondaryButton("Move later", i + 1 < value.size(), nullptr, ImVec2(-1, 0))) { moveFrom = static_cast<int>(i); moveTo = moveFrom + 1; }
                    }
                    kit::cardEnd();
                    if (removed) remove = static_cast<int>(i);
                    ImGui::PopID();
                }
                if (!value.empty() && kit::secondaryButton("Duplicate last entry", true, nullptr, ImVec2(-1, 0))) {
                    auto copy = value.back(); value.push_back(std::move(copy)); changed = true;
                }
                if (remove >= 0) { value.erase(value.begin() + remove); changed = true; }
                else if (moveFrom >= 0) { std::swap(value[moveFrom], value[moveTo]); changed = true; }
            }
            ImGui::TreePop();
        }
    } else if (value.is_boolean()) {
        bool field = value.get<bool>();
        if (kit::fieldCheckbox("value", label, &field)) { value = field; changed = true; }
    } else if (value.is_number_integer()) {
        if (value >= std::numeric_limits<int>::min() && value <= std::numeric_limits<int>::max()) {
            int field = value.get<int>();
            if (kit::fieldInt("value", label, &field)) {
                if (value.is_number_unsigned() && field >= 0) value = static_cast<std::uint64_t>(field);
                else value = field;
                changed = true;
            }
        } else {
            // Keep the complete native integer domain; never round through double.
            std::string field = value.dump();
            if (kit::fieldText("value", label, &field)) {
                auto parsed = nlohmann::json::parse(field, nullptr, false);
                if (parsed.is_number_integer()) { value = std::move(parsed); changed = true; }
                else kit::inlineStatus("Use an integer.", kit::Tone::Err);
            }
        }
    } else if (value.is_number()) {
        double field = value.get<double>();
        if (kit::fieldNumber("value", label, &field, kit::FieldOpts().withUnit(fwScenarioFieldUnit(id)).withSigFigs(12))) {
            value = field; changed = true;
        }
    } else if (value.is_string()) {
        auto& field = value.get_ref<std::string&>();
        changed = kit::fieldText("value", label, &field);
    } else kit::fieldReadout("value", label, "null (edit in raw JSON)");
    ImGui::PopID();
    return changed;
}
inline bool fwScenarioPayloadText(const ImGuiPayload* payload, std::string& text) {
    if (!payload || !payload->IsDelivery() || !payload->Data || payload->DataSize < 2) return false;
    const auto* bytes = static_cast<const char*>(payload->Data);
    if (bytes[payload->DataSize - 1] != '\0' || std::memchr(bytes, '\0', payload->DataSize - 1)) return false;
    text.assign(bytes, static_cast<size_t>(payload->DataSize - 1));
    return true;
}
inline bool drawFwScenarioEditor(FwScenarioEditor& editor, nlohmann::json& canonical,
                                 const char* scope, std::vector<char>* externalRaw = nullptr) {
    ImGui::PushID(scope);
    const ImGuiID editorScope = ImGui::GetID("track_order_scope");
    const bool editable = !(ImGui::GetCurrentContext()->CurrentItemFlags & ImGuiItemFlags_Disabled);
    auto& raw = externalRaw ? *externalRaw : editor.raw;
    if (!editor.initialized || (!editor.draftBlocked && canonical != editor.synchronized))
        syncFwScenarioEditor(editor, canonical, externalRaw);
    bool changed = false;
    bool browse = false;
    kit::fieldPath("scenario_path", "Saved scenario", &editor.path, &browse);
    if (browse) {
        const auto path = ui::openFileDialog("Scenario JSON (*.json)\0*.json\0All files\0*.*\0");
        if (!path.empty()) changed |= loadFwScenarioFile(editor, canonical, path, externalRaw);
    }
    if (kit::secondaryButton("Load scenario file", !editor.path.empty(), "Choose a JSON path", ImVec2(-1, 0)))
        changed |= loadFwScenarioFile(editor, canonical, editor.path, externalRaw);
    if (kit::secondaryButton("Save scenario JSON", !editor.path.empty() && editor.rawValid && canonical.is_object(),
                             "Choose a path and repair the scenario first", ImVec2(-1, 0))) {
        std::ofstream file(editor.path, std::ios::binary);
        if (file) { file << raw.data(); file.close(); }
        editor.error = file ? "" : "Cannot write scenario file.";
    }
    kit::secondaryButton("Drop scenario JSON from Explorer here", true, nullptr, ImVec2(-1, 0));
    if (editable && ImGui::BeginDragDropTarget()) {
        std::string path;
        if (fwScenarioPayloadText(ImGui::AcceptDragDropPayload(ui::kFileDragPayload), path))
            changed |= loadFwScenarioFile(editor, canonical, path, externalRaw);
        ImGui::EndDragDropTarget();
    }
    ImGui::BeginDisabled(editor.draftBlocked);
    kit::sectionHeader("Stimulus palette", nullptr, nullptr, -1, true);
    kit::dimText("Click a stimulus to add it, or drag it to the track list. Each starts as an "
                 "example: set its pins, bus and timing for your board.");
    const char* kinds[] = {"gpio_edges", "uart_rx", "i2c_register_stream", "spi_peer", "can_frames",
                          "button", "quadrature", "i2c_sensor", "i2c_registers", "spi_stub", "adc_waveform"};
    for (int i = 0; i < 11; ++i) {
        if (i == 5 && !ImGui::CollapsingHeader("More stimuli")) break;
        const char* title = fwScenarioKindTitle(kinds[i]);
        ImGui::PushID(i);
        if (kit::secondaryButton(title, true, nullptr, ImVec2(-1, 0))) changed |= addFwScenarioTrack(canonical, kinds[i], editor.error);
        if (ImGui::BeginDragDropSource()) {
            ImGui::SetDragDropPayload(kFwTrackDragPayload, kinds[i], std::strlen(kinds[i]) + 1);
            ImGui::TextUnformatted(title);
            ImGui::EndDragDropSource();
        }
        ImGui::PopID();
    }
    kit::secondaryButton("Drop stimulus here to add track", true, nullptr, ImVec2(-1, 0));
    if (editable && !editor.draftBlocked && ImGui::BeginDragDropTarget()) {
        std::string kind;
        if (fwScenarioPayloadText(ImGui::AcceptDragDropPayload(kFwTrackDragPayload), kind))
            changed |= addFwScenarioTrack(canonical, kind, editor.error);
        ImGui::EndDragDropTarget();
    }
    if (canonical.is_object() && canonical.contains("tracks") && canonical["tracks"].is_array()) {
        auto& tracks = canonical["tracks"];
        int remove = -1, moveFrom = -1, moveTo = -1;
        for (size_t i = 0; i < tracks.size(); ++i) {
            ImGui::PushID(static_cast<int>(i));
            bool removed = false;
            const std::string kind = tracks[i].is_object() && tracks[i].contains("kind") && tracks[i]["kind"].is_string()
                ? tracks[i]["kind"].get<std::string>() : std::string();
            const char* kindTitle = fwScenarioKindTitle(kind);
            const std::string title = "Track " + std::to_string(i + 1) + " \xc2\xb7 " +
                (kindTitle ? std::string(kindTitle) : kind.empty() ? std::string("Invalid track") : kind);
            if (kit::removableCard("track", title.c_str(), nullptr, nullptr, "Remove stimulus track", &removed)) {
                kit::secondaryButton("Drag track to reorder", true, nullptr, ImVec2(-1, 0));
                if (ImGui::BeginDragDropSource()) {
                    struct Order { ImGuiID scope; size_t index; } order { editorScope, i };
                    ImGui::SetDragDropPayload(kFwTrackOrderPayload, &order, sizeof(order));
                    ImGui::TextUnformatted(title.c_str());
                    ImGui::EndDragDropSource();
                }
                if (editable && !editor.draftBlocked && ImGui::BeginDragDropTarget()) {
                    struct Order { ImGuiID scope; size_t index; };
                    if (const auto* payload = ImGui::AcceptDragDropPayload(kFwTrackOrderPayload)) {
                        if (payload->IsDelivery() && payload->Data && payload->DataSize == sizeof(Order)) {
                            Order order; std::memcpy(&order, payload->Data, sizeof(order));
                            if (order.scope == editorScope && order.index < tracks.size()) {
                                moveFrom = static_cast<int>(order.index);
                                moveTo = static_cast<int>(i);
                            }
                        }
                    }
                    ImGui::EndDragDropTarget();
                }
                if (kit::secondaryButton("Move track earlier", i > 0, nullptr, ImVec2(-1, 0))) { moveFrom = static_cast<int>(i); moveTo = moveFrom - 1; }
                if (kit::secondaryButton("Move track later", i + 1 < tracks.size(), nullptr, ImVec2(-1, 0))) { moveFrom = static_cast<int>(i); moveTo = moveFrom + 1; }
                changed |= drawFwScenarioValue("parameters", "Target, timing and events", tracks[i]);
            }
            kit::cardEnd();
            if (removed) remove = static_cast<int>(i);
            ImGui::PopID();
        }
        if (remove >= 0) { tracks.erase(tracks.begin() + remove); changed = true; }
        else if (moveFrom >= 0) changed |= moveFwScenarioTrack(canonical, moveFrom, moveTo, editor.error);
        if (ImGui::CollapsingHeader("Scenario metadata and electrical links")) {
            kit::dimText("Settings stored with the scenario. Unrecognized settings are kept; "
                         "only what the emulator supports affects the run.");
            for (auto& item : canonical.items())
                if (item.key() != "tracks")
                    changed |= drawFwScenarioValue(item.key().c_str(), fwScenarioKeyLabel(item.key()).c_str(), item.value());
        }
    }
    ImGui::EndDisabled();
    if (changed) syncFwScenarioEditor(editor, canonical, externalRaw);
    if (ImGui::CollapsingHeader("Full native scenario JSON", ImGuiTreeNodeFlags_DefaultOpen)) {
        if (ImGui::InputTextMultiline("raw_scenario", raw.data(), raw.size(),
                                     ImVec2(-1, ImGui::GetFrameHeightWithSpacing() * 6),
                                     ImGuiInputTextFlags_CallbackResize, fwScenarioResizeInput, &raw)) {
            nlohmann::json candidate;
            const std::string text(raw.data());
            // Fixture references retain the native convenience path but have no
            // structured track representation; the backend resolves them on run.
            if (parseFwScenarioText(text, candidate, editor.validationError)) {
                canonical = std::move(candidate);
                editor.rawValid = true;
                editor.draftBlocked = fwScenarioFixtureReference(canonical);
                editor.synchronized = canonical;
            } else {
                editor.rawValid = false;
                editor.draftBlocked = true;
            }
            changed = true;
        }
        // A raw editor is also a file target, so the obvious drop works directly.
        if (editable && ImGui::BeginDragDropTarget()) {
            std::string path;
            if (fwScenarioPayloadText(ImGui::AcceptDragDropPayload(ui::kFileDragPayload), path))
                changed |= loadFwScenarioFile(editor, canonical, path, externalRaw);
            ImGui::EndDragDropTarget();
        }
    }
    if (editor.draftBlocked)
        kit::inlineStatus(editor.rawValid ? "Fixture reference: edit raw JSON or load a scenario to use track controls."
                                         : "Raw draft is invalid. Track edits are paused so the draft cannot be overwritten.", kit::Tone::Warn);
    if (!editor.validationError.empty()) kit::inlineStatus(editor.validationError.c_str(), kit::Tone::Err);
    if (!editor.error.empty()) kit::inlineStatus(editor.error.c_str(), kit::Tone::Err);
    kit::dimText("Native fw_sim_run validates backend, controller and electrical links. No scenario key is translated into a GUI-only dialect.");
    ImGui::PopID();
    return changed;
}
} // namespace ui::sim
