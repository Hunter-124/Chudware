// CFD / kinematic / flight-profile → aicad.fwscenario/1 sensor bridge (C++
// port of engine_oracle/firmware/sim_sensor_bridge.py). Samples the profile
// onto a uniform grid and packs device register bytes into time-varying I2C
// tracks (uart_rx NMEA for GNSS). Physics lives in sensor_models.*.
#pragma once

#include <optional>
#include <string>

#include <nlohmann/json.hpp>

#include "firmware/sensor_models.h"

namespace chudware::firmware {

// Build a validated aicad.fwscenario/1 dict from a FlightProfile / trajectory
// dict / CFD summary + sensor map. Time inputs are seconds; generated track times
// are milliseconds. Invalid/non-finite setup and outputs above 10000 samples throw
// std::invalid_argument / std::runtime_error before track allocation.
nlohmann::json build_sensor_scenario(const nlohmann::json& sim_result_or_profile,
                                     const nlohmann::json& sensor_map,
                                     const nlohmann::json& duration_s,  // null → profile span
                                     double dt_s, const std::string& name);

}  // namespace chudware::firmware
