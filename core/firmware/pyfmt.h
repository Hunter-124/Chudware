// Python-formatting helpers — the firmware pillar's generated monitor lines
// and validation error messages embed Python-formatted values (f"{v:g}",
// f"{v:.3f}", f"{x!r}", f"{x:#x}"), and the parity bar is byte-identical
// strings. Centralised so scenario/emulator/powerprofile/sensor bridge agree.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace aicad::firmware {

// Python f"{v:g}" (== C %g: 6 significant digits, trailing zeros stripped).
std::string fmt_g(double v);

// Python f"{v:.3f}".
std::string fmt_3f(double v);

// Python repr() of a JSON value: None/True/False, 'single-quoted' strings,
// 5 vs 5.0 number repr, recursive [list, ...] / {'k': v} rendering.
std::string py_repr(const nlohmann::json& v);

// Python f"{x:#x}".
std::string fmt_hex(long long v);

}  // namespace aicad::firmware
