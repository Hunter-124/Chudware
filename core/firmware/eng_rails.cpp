#include "firmware/eng_rails.h"

#include <map>

namespace chudware::firmware {
namespace {

nlohmann::json rail(const char* name, nlohmann::json vmin, nlohmann::json vnom,
                    nlohmann::json vmax, nlohmann::json iact, nlohmann::json islp) {
    return {{"name", name},           {"voltage_v_min", vmin}, {"voltage_v_nom", vnom},
            {"voltage_v_max", vmax},  {"current_a_active", iact},
            {"current_a_sleep", islp}};
}

const nlohmann::json NIL;

// Starter-library symbol eng supply.rails, probed from
// engine_oracle/ecad/library/starter.py at port time (2026-07-22).
const std::map<std::string, nlohmann::json> kSymbolEngRails = {
    {"Amplifier_Operational:OPAMP_dual", nlohmann::json::array({
        rail("V+", 3.0, 5.0, 32.0, 0.0007, NIL)})},
    {"Timer:NE555", nlohmann::json::array({
        rail("VCC", 4.5, 5.0, 16.0, 0.003, NIL)})},
    {"Regulator_Linear:AMS1117-3.3", nlohmann::json::array({
        rail("VIN", 4.75, 5.0, 12.0, 0.01, NIL),
        rail("VOUT", 3.2, 3.3, 3.4, NIL, NIL)})},
    {"Regulator_Linear:7805", nlohmann::json::array({
        rail("VIN", 7.0, 9.0, 25.0, 0.005, NIL),
        rail("VOUT", 4.8, 5.0, 5.2, NIL, NIL)})},
    {"MCU:Generic_QFP48", nlohmann::json::array({
        rail("VDD", 2.0, 3.3, 3.6, 0.02, 1e-05),
        rail("VDDA", 2.0, 3.3, 3.6, 0.001, NIL)})},
    {"Sensor_Motion:MPU6050", nlohmann::json::array({
        rail("VDD", 2.375, 3.3, 3.46, 0.0039, 5e-06),
        rail("VLOGIC", 1.71, 3.3, 3.46, 0.0001, NIL)})},
    {"Sensor_Pressure:BMP280", nlohmann::json::array({
        rail("VDD", 1.71, 3.3, 3.6, 2.7e-06, 1e-07),
        rail("VDDIO", 1.2, 3.3, 3.6, 1e-07, NIL)})},
    {"Sensor_Magnetic:LIS3MDL", nlohmann::json::array({
        rail("VDD", 1.9, 3.3, 3.6, 0.00027, 1e-06),
        rail("VDD_IO", 1.71, 3.3, 3.6, 1e-05, NIL)})},
    {"RF_GPS:NEO6M", nlohmann::json::array({
        rail("VCC", 2.7, 3.3, 3.6, 0.045, 2e-05)})},
    {"Regulator_Switching:Buck_3V3", nlohmann::json::array({
        rail("VIN", 4.5, 5.0, 17.0, 0.001, NIL),
        rail("VOUT", 3.2, 3.3, 3.4, NIL, NIL)})},
    {"Battery_Management:MCP73831", nlohmann::json::array({
        rail("VDD", 3.75, 5.0, 6.0, 0.00055, NIL),
        rail("VBAT", 0.0, 4.2, 4.2, NIL, NIL)})},
    {"Battery_Management:SwitchingCharger_SOP8", nlohmann::json::array({
        rail("VIN", 4.5, 12.0, 28.0, 0.002, NIL),
        rail("BAT", 0.0, 4.2, 4.2, NIL, NIL)})},
    {"Power_Management:Load_Switch", nlohmann::json::array({
        rail("VIN", 1.0, 3.3, 5.5, 1e-05, NIL)})},
    {"Power_Management:LVD_Comparator", nlohmann::json::array({
        rail("VDD", 1.6, 3.3, 5.5, 1e-05, NIL)})},
    {"FPGA:iCE40UP5K-SG48", nlohmann::json::array({
        rail("VCC", 1.14, 1.2, 1.26, NIL, NIL),
        rail("VCCIO", 1.71, 3.3, 3.465, NIL, NIL)})},
};

const char* kRailKeys[] = {"name", "voltage_v_min", "voltage_v_nom",
                           "voltage_v_max", "current_a_active", "current_a_sleep"};

// Normalize one raw rail dict to the full 6-key envelope (missing -> null),
// mirroring metadata._normalize_rails.
nlohmann::json normalize_rail(const nlohmann::json& r) {
    nlohmann::json out;
    for (const char* k : kRailKeys)
        out[k] = (r.is_object() && r.contains(k)) ? r[k] : nlohmann::json(nullptr);
    return out;
}

}  // namespace

std::vector<nlohmann::json> eng_supply_rails(const nlohmann::json& fp) {
    auto rails_of = [](const nlohmann::json& eng, bool& present) -> std::vector<nlohmann::json> {
        present = false;
        std::vector<nlohmann::json> out;
        if (!eng.is_object()) return out;
        auto sup = eng.find("supply");
        if (sup == eng.end() || !sup->is_object()) return out;
        auto rails = sup->find("rails");
        if (rails == sup->end() || !rails->is_array()) return out;
        present = true;  // an (even empty) rails list REPLACES the library list
        for (const auto& r : *rails)
            if (r.is_object()) out.push_back(normalize_rail(r));
        return out;
    };
    // Per-instance overrides win (eng_overrides, else fields.eng).
    nlohmann::json overrides;
    if (fp.contains("eng_overrides") && fp["eng_overrides"].is_object())
        overrides = fp["eng_overrides"];
    else if (fp.contains("fields") && fp["fields"].is_object() &&
             fp["fields"].contains("eng") && fp["fields"]["eng"].is_object())
        overrides = fp["fields"]["eng"];
    bool present = false;
    if (auto rails = rails_of(overrides, present); present) return rails;
    auto it = kSymbolEngRails.find(fp.value("libid", std::string()));
    if (it != kSymbolEngRails.end()) {
        std::vector<nlohmann::json> out;
        for (const auto& r : it->second) out.push_back(normalize_rail(r));
        return out;
    }
    return {};
}

}  // namespace chudware::firmware
