// sim_pump_drive — the ONE electric pump-drive form shared by the Piping panel (Solve > Pump drive)
// and the Hydraulics panel (Sizing > Drive). Both size the motor through the engine's
// pump_drive_size tool; this file owns its inputs, the request params and the result card so the
// two surfaces cannot drift apart.
//
//   ui::sim::PumpDriveInputs in;                        // panel member (defaults below)
//   ui::sim::drawPumpDriveInputs("drive", in, true);   // kind + duty + pump + motor cards
//   st.rpc.call("pump_drive_size", ui::sim::pumpDriveParams(in), ...);
//   ui::sim::drawPumpDriveResult("drive_res", result);
//
// GL/GUI thread only (the draw functions).
#pragma once

#include <nlohmann/json.hpp>

namespace ui {
namespace sim {

struct PumpDriveInputs {
    int kind = 0;                 // 0 hydraulic (flow + pressure), 1 centrifugal (flow + head)
    double flowLpm = 40.0;        // hydraulic duty flow, L/min
    double pressureBar = 200.0;   // hydraulic duty pressure (the relief setting), bar
    double flowM3h = 10.0;        // centrifugal duty flow, m³/h
    double headM = 30.0;          // centrifugal duty head, m
    double densityKgm3 = 998.0;   // fluid density, kg/m³
    double pumpEff = 0.7;         // overall pump efficiency at the duty point, 0..1
    double speedRpm = 1500.0;     // required pump speed (picks the motor pole count)
    double serviceFactor = 1.15;
    double voltageV = 400.0;
    double frequencyHz = 50.0;
    int standard = 0;             // 0 IEC, 1 NEMA
    int phases = 3;               // 1 or 3
    int effClass = 1;             // 0 IE2, 1 IE3, 2 IE4
};

// pump_drive_size params for these inputs: kind, the kind's duty pair (flow_lpm + pressure_bar,
// or flow_m3h + head_m), fluid density, pump efficiency/speed and the motor settings.
nlohmann::json pumpDriveParams(const PumpDriveInputs& in);

// Kit field cards for the inputs, scoped under `id`. showKindAndDuty draws the kind switch and
// the duty point (flow/pressure or flow/head + density) in the "Pump duty" card; without it the
// card holds only the pump efficiency and speed (the caller supplies the duty point, e.g. a
// solved network). Always draws the "Motor" card. Returns true when any field committed.
bool drawPumpDriveInputs(const char* id, PumpDriveInputs& in, bool showKindAndDuty);

// Result card of one pump_drive_size answer (scoped under `id`), titled `title` (null = "Pump
// drive"; callers name the pump's run/component in human words, never a raw id): duty flow and
// head (when the result carries `operating_point`), powers, motor rating, poles, synchronous
// speed, efficiency, current and frame, then its warnings. Nothing when `result` is not a
// non-empty object.
void drawPumpDriveResult(const char* id, const nlohmann::json& result, const char* title = nullptr);

}  // namespace sim
}  // namespace ui
