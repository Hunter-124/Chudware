// Section library lookups for the structural workstream.
//
// C++ port of engine_oracle/structural/sections.py: AISC W/S/C steel section
// properties and dressed (S4S) lumber section properties, from the curated
// tables embedded as data_aisc.h / data_lumber.h (generated from the oracle's
// JSON — dimensional facts, license-clean per contracts/licenses.md).
//
// Units: mm / mm^2 / mm^3 / mm^4 / mm^6 — the engine's canonical N·mm·MPa system.
#pragma once

#include <optional>
#include <string>

#include <nlohmann/json.hpp>

namespace chudware::structural {

struct AiscShape {
    const char* name;  // designation, uppercase, no spaces (e.g. "W8X31")
    double A, d, bf, tf, tw, Ix, Sx, Zx, Iy, Sy, Zy, J, Cw;
};

struct LumberShape {
    const char* name;  // nominal size, lowercase (e.g. "2x4")
    double b_actual_mm, d_actual_mm, A_mm2, Ix_mm4, Sx_mm3, Iy_mm4, Sy_mm3;
};

// AISC shape lookup — case-insensitive, internal spaces ignored (mirrors
// get_aisc_section). Returns the property dict {A, d, bf, tf, tw, Ix, Sx, Zx,
// Iy, Sy, Zy, J, Cw} or std::nullopt when the designation is not in the table.
std::optional<nlohmann::json> get_aisc_section(const std::string& name);

// Dressed-lumber lookup — case-insensitive, internal spaces ignored (mirrors
// get_lumber_section). Returns {b_actual_mm, d_actual_mm, A, Ix, Sx, Iy, Sy}
// or std::nullopt when the nominal size is not in the table.
std::optional<nlohmann::json> get_lumber_section(const std::string& nominal);

}  // namespace chudware::structural
