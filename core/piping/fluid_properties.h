// core/piping/fluid_properties.h — sourced thermophysical fluid properties.
// C++ port of engine_oracle/piping/properties.py: density rho (kg/m3),
// dynamic viscosity mu (Pa·s) and vapour pressure pvap (Pa) for a named fluid
// at T (K), P (Pa). The Python module feature-detects CoolProp/thermo and
// falls back to a cited built-in model; the C++ port implements the built-in
// model only (backend is always "builtin" — the same answer the oracle gives
// on any box without those optional packages, which is the shipping config:
// neither is in requirements.txt). No value is ever fabricated: an unknown
// fluid or out-of-range condition degrades contract-shaped.
#pragma once

#include "piping/pyutil.h"

namespace aicad::piping {

using pyutil::ojson;

inline constexpr double R_UNIVERSAL = 8.314462618;  // J/(mol·K), CODATA 2018
inline constexpr double T0_C = 273.15;              // 0 °C in K
inline constexpr double P_ATM = 101325.0;           // standard atmosphere, Pa
inline constexpr double STD_T = 288.15;             // 15 °C — ISO 13443 standard
inline constexpr double STD_P = P_ATM;

// Canonical fluid name for an alias, or "" when unknown.
std::string canon_fluid(const std::string& name);

// {"ok": true, "name", "phase", "T_K", "P_Pa", "rho", "mu", "pvap",
//  "molar_mass", "source", "backend"} or contract-shaped {"ok": false, ...}.
// `prefer` is accepted for interface parity; every backend here is "builtin".
ojson fluid_properties(const ojson& name, double T = 293.15, double P = P_ATM,
                       const ojson& prefer = nullptr);

// Normalise a solver `fluid` argument (null / {rho,mu} / {name,T,P}) into
// {rho, mu, name, source, ...} — or a contract-shaped failure, surfaced
// unchanged by the solver.
ojson resolve_solver_fluid(const ojson& fluid);

}  // namespace aicad::piping
