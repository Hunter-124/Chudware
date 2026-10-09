// sim_pump_drive — the ONE electric pump-drive form shared by the Piping panel (Solve > Pump drive)
// and the Hydraulics panel (Sizing > Drive). Both size the motor through the engine's
// pump_drive_size tool; this file owns its inputs, the request params and the result card so the
// two surfaces cannot drift apart.
//
//   ui::sim::PumpDriveInputs in;                            // panel member (defaults below)
//   ui::sim::drawPumpDriveInputs("drive", in, true, us);   // kind + duty + pump + motor cards
//   st.rpc.call("pump_drive_size", ui::sim::pumpDriveParams(in), ...);
//   ui::sim::drawPumpDriveResult("drive_res", result, "Drive sizing", us);
//
// `us` is the Simulation pane's unit system (SimManager::unitSys(st).us): US customary shows and
// takes the duty in gpm / psi / ft / lb/ft³ and adds hp beside every kW; inputs and the request
// stay SI.
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
    // Mains supply. These are only a starting point: defaultSupplyForUnits() picks the regional
    // supply that matches the Simulation pane's units the first time the form is shown, and the
    // Motor card leads with an explicit "Mains supply" choice.
    double voltageV = 400.0;
    double frequencyHz = 50.0;
    int standard = 0;             // 0 IEC, 1 NEMA
    int phases = 3;               // 1 or 3
    int effClass = 1;             // 0 IE2, 1 IE3, 2 IE4
    bool supplyChosen = false;    // set once a supply was picked (by the user or a region default)
    bool customSupply = false;    // the user chose "Custom" supply (shows the individual fields)
};

// Region default for a form whose supply nobody has chosen yet: US customary units get the
// North American 460 V / 60 Hz NEMA supply, SI units 400 V / 50 Hz IEC. A supply the
// user already picked is left alone.
void defaultSupplyForUnits(PumpDriveInputs& in, bool usCustomary);

// pump_drive_size params for these inputs: kind, the kind's duty pair (flow_lpm + pressure_bar,
// or flow_m3h + head_m), fluid density, pump efficiency/speed and the motor settings.
nlohmann::json pumpDriveParams(const PumpDriveInputs& in);

// Kit field cards for the inputs, scoped under `id`. showKindAndDuty draws the kind switch and
// the duty point (flow/pressure or flow/head + density) in the "Pump duty" card; without it the
// card holds only the pump efficiency and speed (the caller supplies the duty point, e.g. a
// solved network). Always draws the "Motor" card. Returns true when any field committed.
bool drawPumpDriveInputs(const char* id, PumpDriveInputs& in, bool showKindAndDuty, bool us);

// Result card of one pump_drive_size answer (scoped under `id`), titled `title` (null = "Pump
// drive"; callers name the pump's run/component in human words, never a raw id): duty flow and
// head (when the result carries `operating_point`), powers, motor rating, poles, synchronous
// speed, efficiency, current and frame, then its warnings. Nothing when `result` is not a
// non-empty object.
void drawPumpDriveResult(const char* id, const nlohmann::json& result, const char* title, bool us);

}  // namespace sim
}  // namespace ui
