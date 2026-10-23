// Emulation power/duty-cycle profile — aicad.powerprofile/1 (C++ port of
// engine_oracle/firmware/powerprofile.py). BOOKKEEPING, not SPICE: datasheet
// envelope currents scaled by observed/derived duty cycles; every figure names
// its source heuristic; unknown currents stay null. contracts/firmware_rpc.md
// §11. Pure except write_profile_artifact (atomic, best-effort).
#pragma once

#include <optional>
#include <string>

#include <nlohmann/json.hpp>

namespace chudware::firmware {

inline constexpr const char* kPowerProfileSchema = "aicad.powerprofile/1";
inline constexpr const char* kPowerModelNote =
    "duty-cycle bookkeeping over datasheet rail currents — "
    "NOT a SPICE/instruction-level power simulation";

// Assemble the aicad.powerprofile/1 artifact dict. `trace` is a trace artifact
// dict (or SimResult-shaped dict); `board` an ECAD board doc or null;
// window_ms <= 0 means "derive from the trace".
nlohmann::json build_power_profile(const nlohmann::json& trace, const nlohmann::json& board,
                                   const std::string& mcu_family,
                                   const nlohmann::json& trace_ref, double window_ms = 0.0);

// Write the profile next to the ELF as <elf>.power.json (atomic tmp+replace;
// null on any failure or when the ELF does not exist).
std::optional<std::string> write_profile_artifact(const nlohmann::json& profile,
                                                  const std::string& elf_path);

}  // namespace chudware::firmware
