// Firmware RPC dispatch (T-RF15) — the single entry the libaicad integrator
// wires into core/api/libaicad.cpp. Mirrors the frozen surface of
// contracts/firmware_rpc.md §5/§8 as implemented by
// engine_oracle/firmware/mcp_tools.py + worker_rpc.py: dict in → dict out,
// expected failures come back INSIDE the result as the {code, message, detail}
// error envelope, never raised.
//
// Ported methods: fw_new_project, fw_build, fw_sources, fw_sim_run (incl. §7
// trace artifact + §9 scenarios + §11 power-profile sidecar), fw_verify_board
// (static + trace/emulation dynamic passes), fw_power_profile,
// fw_sensor_scenario, fw_flash, fw_debug_start, fw_debug_halt,
// fw_debug_read_mem, fw_status, fw_projects. The whole live surface — the
// dispatch returns false only for genuinely unknown methods.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace aicad::firmware {

// Dispatch one fw_* method. Returns false when `method` is not a ported
// firmware method (caller falls through to the next dispatch layer / the
// Python worker). Returns true when handled: `result` holds either the
// method's result or the {code, message, detail} error envelope. `err` is
// reserved for dispatch-level failures and stays empty today (the firmware
// contract routes all expected failures into `result`).
bool call(const std::string& method, const nlohmann::json& params, nlohmann::json& result,
          std::string& err);

}  // namespace aicad::firmware
