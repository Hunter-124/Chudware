// sim_pump_drive — see sim_pump_drive.h.
#include "sim_pump_drive.h"

#include <algorithm>
#include <string>
#include <vector>

#include "imgui.h"

#include "../widgets/kit.h"
#include "sim_common.h"

namespace ui {
namespace sim {
namespace {

using json = nlohmann::json;

const char* const kKinds[] = {"Hydraulic", "Centrifugal"};
const char* const kKindTips[] = {"Positive-displacement hydraulic pump: duty is flow and pressure.",
                                 "Centrifugal pump: duty is flow and head."};
const char* const kStandards[] = {"IEC", "NEMA"};
const char* const kStandardTips[] = {"IEC motors: kW ratings and IEC frames.",
                                     "NEMA motors: hp ratings and NEMA frames."};
const char* const kPhases[] = {"Single-phase", "Three-phase"};
const char* const kEffClasses[] = {"IE2", "IE3", "IE4"};

// Common mains supplies; the last entry (Custom) shows the individual supply fields.
struct Supply {
    const char* label;
    double volts, hz;
    int phases, standard;  // standard: 0 IEC, 1 NEMA
};
const Supply kSupplies[] = {
    {"460 V, 60 Hz, three-phase (North America)", 460.0, 60.0, 3, 1},
    {"230 V, 60 Hz, three-phase (North America)", 230.0, 60.0, 3, 1},
    {"400 V, 50 Hz, three-phase (IEC regions)", 400.0, 50.0, 3, 0},
    {"230 V, 50 Hz, single-phase (IEC regions)", 230.0, 50.0, 1, 0},
};
constexpr int kNSupplies = (int)(sizeof(kSupplies) / sizeof(kSupplies[0]));
const char* const kSupplyLabels[kNSupplies + 1] = {kSupplies[0].label, kSupplies[1].label,
                                                   kSupplies[2].label, kSupplies[3].label, "Custom"};

// The shown supply: Custom when the user chose it (even if the numbers match a preset), else
// the preset matching the current values, else Custom.
int supplyIndex(const PumpDriveInputs& in) {
    if (in.customSupply) return kNSupplies;
    for (int i = 0; i < kNSupplies; ++i)
        if (in.voltageV == kSupplies[i].volts && in.frequencyHz == kSupplies[i].hz &&
            in.phases == kSupplies[i].phases)
            return i;
    return kNSupplies;  // Custom
}

// "12.3 kW" from a numeric member, or an em dash when absent.
std::string qty(const json& o, const char* k, kit::Unit u) {
    return jhas(o, k) ? kit::fmtQuantity(jnum(o, k), kit::unitText(u)) : std::string("\xe2\x80\x94");
}

}  // namespace

void defaultSupplyForUnits(PumpDriveInputs& in, bool usCustomary) {
    if (in.supplyChosen) return;
    const Supply& s = kSupplies[usCustomary ? 0 : 2];
    in.voltageV = s.volts;
    in.frequencyHz = s.hz;
    in.phases = s.phases;
    in.standard = s.standard;
    in.supplyChosen = true;
}

nlohmann::json pumpDriveParams(const PumpDriveInputs& in) {
    json p = {{"kind", in.kind == 0 ? "hydraulic" : "centrifugal"},
              {"fluid_density_kgm3", in.densityKgm3},
              {"pump_efficiency", in.pumpEff},
              {"speed_rpm", in.speedRpm},
              {"standard", kStandards[std::clamp(in.standard, 0, 1)]},
              {"service_factor", in.serviceFactor},
              {"voltage_v", in.voltageV},
              {"phases", in.phases},
              {"frequency_hz", in.frequencyHz},
              {"efficiency_class", kEffClasses[std::clamp(in.effClass, 0, 2)]}};
    if (in.kind == 0) {
        p["flow_lpm"] = in.flowLpm;
        p["pressure_bar"] = in.pressureBar;
    } else {
        p["flow_m3h"] = in.flowM3h;
        p["head_m"] = in.headM;
    }
    return p;
}

bool drawPumpDriveInputs(const char* id, PumpDriveInputs& in, bool showKindAndDuty) {
    ImGui::PushID(id && id[0] ? id : "pump_drive");
    bool committed = false;

    if (kit::cardBegin("duty", "Pump duty", "size_pump")) {
        kit::FieldGroup g("duty_fields", "Pump efficiency");
        if (showKindAndDuty) {
            int kind = std::clamp(in.kind, 0, 1);
            if (kit::fieldEnum("kind", "Pump kind", &kind, kKinds, 2,
                               kit::FieldOpts().withItemTips(kKindTips))) {
                in.kind = kind;
                committed = true;
            }
            if (in.kind == 0) {
                committed |= kit::fieldNumber("flow_lpm", "Flow", &in.flowLpm,
                                              kit::FieldOpts().withUnit(kit::Unit::L_min).withMin(0.0)
                                                  .withRequired());
                committed |= kit::fieldNumber("pressure_bar", "Pressure", &in.pressureBar,
                                              kit::FieldOpts().withUnit(kit::Unit::bar).withMin(0.0)
                                                  .withRequired()
                                                  .withTip("Highest working pressure (the relief setting)."));
            } else {
                committed |= kit::fieldNumber("flow_m3h", "Flow", &in.flowM3h,
                                              kit::FieldOpts().withUnit(kit::Unit::m3_h).withMin(0.0)
                                                  .withRequired());
                committed |= kit::fieldNumber("head_m", "Head", &in.headM,
                                              kit::FieldOpts().withUnit(kit::Unit::m).withMin(0.0)
                                                  .withRequired());
            }
            committed |= kit::fieldNumber("density", "Fluid density", &in.densityKgm3,
                                          kit::FieldOpts().withUnit(kit::Unit::kg_m3).withMin(1.0)
                                              .withHint("998"));
        }
        committed |= kit::fieldNumber("eff", "Pump efficiency", &in.pumpEff,
                                      kit::FieldOpts().withScale(100.0).withUnit(kit::Unit::Percent)
                                          .withRange(0.01, 1.0).withSigFigs(3).withHint("70")
                                          .withTip("Overall pump efficiency at the duty point."));
        committed |= kit::fieldNumber("speed", "Speed", &in.speedRpm,
                                      kit::FieldOpts().withUnit(kit::Unit::rpm).withMin(1.0)
                                          .withHint("1500")
                                          .withTip("Required pump speed: picks the motor pole count."));
    }
    kit::cardEnd();

    if (kit::cardBegin("motor", "Motor", "pump_drive")) {
        {
            // The supply decides voltage, frequency and the motor standard together, so it is
            // the first, explicit choice; Custom reveals the individual fields.
            kit::FieldGroup g("supply_fields");
            int supply = supplyIndex(in);
            if (kit::fieldEnum("supply", "Mains supply", &supply, kSupplyLabels, kNSupplies + 1,
                               kit::FieldOpts()
                                   .withEnumStyle(kit::EnumStyle::Combo)
                                   .withTip("Electrical supply the motor runs from. Sets voltage, "
                                            "frequency, phases and the motor standard; choose "
                                            "Custom to enter them yourself."))) {
                if (supply >= 0 && supply < kNSupplies) {
                    in.voltageV = kSupplies[supply].volts;
                    in.frequencyHz = kSupplies[supply].hz;
                    in.phases = kSupplies[supply].phases;
                    in.standard = kSupplies[supply].standard;
                }
                in.customSupply = supply == kNSupplies;
                in.supplyChosen = true;
                committed = true;
            }
        }
        kit::FieldGroup g("motor_fields", "Efficiency class");
        if (supplyIndex(in) == kNSupplies) {
            committed |= kit::fieldNumber("voltage", "Voltage", &in.voltageV,
                                          kit::FieldOpts().withUnit(kit::Unit::V).withMin(1.0));
            int phase = in.phases == 1 ? 0 : 1;
            if (kit::fieldEnum("phases", "Phases", &phase, kPhases, 2)) {
                in.phases = phase == 0 ? 1 : 3;
                committed = true;
            }
            committed |= kit::fieldNumber("freq", "Frequency", &in.frequencyHz,
                                          kit::FieldOpts().withUnit(kit::Unit::Hz).withMin(1.0));
        }
        int standard = std::clamp(in.standard, 0, 1);
        if (kit::fieldEnum("standard", "Standard", &standard, kStandards, 2,
                           kit::FieldOpts().withItemTips(kStandardTips))) {
            in.standard = standard;
            committed = true;
        }
        committed |= kit::fieldNumber("sf", "Service factor", &in.serviceFactor,
                                      kit::FieldOpts().withRange(1.0, 2.0).withHint("1.15")
                                          .withTip("Margin applied to the shaft power before picking a rating."));
        int cls = std::clamp(in.effClass, 0, 2);
        if (kit::fieldEnum("class", "Efficiency class", &cls, kEffClasses, 3,
                           kit::FieldOpts().withTip("IEC 60034-30-1 motor efficiency class "
                                                    "(IE3 is comparable to NEMA Premium)."))) {
            in.effClass = cls;
            committed = true;
        }
    }
    kit::cardEnd();

    ImGui::PopID();
    return committed;
}

void drawPumpDriveResult(const char* id, const nlohmann::json& r, const char* title) {
    if (!r.is_object() || r.empty()) return;
    ImGui::PushID(id && id[0] ? id : "pump_drive_result");
    std::vector<kit::KeyValueItem> rows;
    const json& op = jobj(r, "operating_point");
    // One fact per row: the duty point's flow and head, then powers, the motor and its speed.
    if (jhas(op, "flow_m3h")) rows.push_back({"Duty flow", qty(op, "flow_m3h", kit::Unit::m3_h)});
    if (jhas(op, "head_m")) rows.push_back({"Duty head", qty(op, "head_m", kit::Unit::m)});
    if (jhas(r, "hydraulic_power_kw"))
        rows.push_back({"Hydraulic power", qty(r, "hydraulic_power_kw", kit::Unit::kW)});
    rows.push_back({"Shaft power", qty(r, "shaft_power_kw", kit::Unit::kW)});
    if (jhas(r, "required_motor_kw"))
        rows.push_back({"Required (with SF)", qty(r, "required_motor_kw", kit::Unit::kW)});
    // The rating is the answer, not a warning: primary ink (warnings are their own lines below).
    if (jhas(r, "motor_rating_kw")) rows.push_back({"Motor rating", qty(r, "motor_rating_kw", kit::Unit::kW)});
    if (jhas(r, "motor_rating_hp")) rows.push_back({"Motor rating", qty(r, "motor_rating_hp", kit::Unit::hp)});
    rows.push_back({"Poles", fmtText(r, "poles")});
    rows.push_back({"Synchronous speed", qty(r, "sync_speed_rpm", kit::Unit::rpm)});
    if (jhas(r, "motor_efficiency"))
        rows.push_back({"Motor efficiency",
                        kit::fmtQuantity(jnum(r, "motor_efficiency") * 100.0,
                                         kit::unitText(kit::Unit::Percent), 3)});
    if (jhas(r, "input_power_kw")) rows.push_back({"Input power", qty(r, "input_power_kw", kit::Unit::kW)});
    rows.push_back({"Full-load current", qty(r, "full_load_current_a", kit::Unit::A)});
    if (r.contains("iec_frame")) rows.push_back({"IEC frame", fmtText(r, "iec_frame")});
    if (r.contains("nema_frame")) rows.push_back({"NEMA frame", fmtText(r, "nema_frame")});
    kit::keyValueCard("card", title && title[0] ? title : "Pump drive", "pump_drive", rows);
    warningsList(r);
    ImGui::PopID();
}

}  // namespace sim
}  // namespace ui
