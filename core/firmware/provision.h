// Firmware toolchain/emulator provisioning doctor — fw_status (C++ port of
// engine_oracle/firmware/provision.py). Pure, offline-safe, NEVER throws: a
// machine with zero firmware tools still gets {ok:true} with every tool
// found=false plus an actionable install_hint. Shape is frozen for callers
// (services/mcp firmware_tools fw_status; the host Firmware workspace gates
// Build/Emulate/Flash/Debug buttons on families.*.can_*).
#pragma once

#include <nlohmann/json.hpp>

namespace chudware::firmware {

// {ok, tools, families, platforms, banners, missing} — see
// engine_oracle/firmware/provision.py module docstring for the frozen shape.
nlohmann::json fw_status();

}  // namespace chudware::firmware
