// core/piping/spec.h — ASME B31.3 pressure design + B16.5 flange rating +
// support-span check for an aicad.piping/1 document (contract RPC
// `piping_spec_check`, contracts/piping_schema.md §8.2). Pure C++.
//
// Data: basic allowable stress S from B31.3 Table A-1 (carbon steel rows) /
// BPVC II-D Table 1A lower row (austenitic: the conservative values, not the
// higher 90 %-of-yield row the code permits where slight deformation is
// acceptable), linearly interpolated in temperature; B16.5 Table 2 P-T
// ratings for material groups 1.1 (carbon steel), 2.1 (304) and 2.2 (316).
#pragma once

#include <string>

#include "piping/pyutil.h"

namespace chudware::piping {

using pyutil::ojson;

// params: {piping, design_pressure_kpa, design_temp_c=40,
//          corrosion_allowance_mm=1.5, mill_tolerance_frac=0.125,
//          joint_factor=1.0, fluid_density_kgm3=1000, insulation_kgm=0}
// Success: {ok:true, code:"ASME B31.3", design, runs:[...], flanges:[...],
//           all_pass, warnings, hint, next_actions}; refusal
// {ok:false, reason, hint} with reason in invalid_params / invalid_piping /
// unknown_material / unknown_pipe_size / temperature_out_of_range /
// external_pressure_not_covered / unknown_flange_class.
ojson piping_spec_check(const ojson& params);

// Basic allowable stress at temp_c: {ok:true, material (canonical),
// allowable_mpa, temp_c, below_min_temp, flange_group, modulus_gpa,
// stainless} or {ok:false, reason:"unknown_material"|"temperature_out_of_range", hint}.
// Accepts the schema names (A106-B, A53-B, A333-6, A312-304, A312-316) and
// the A312-TP304 / A312-TP316 spellings.
ojson b31_3_allowable(const std::string& material, double temp_c);

// B16.5 P-T rating (kPa gauge) for flange_class (150…2500) and material
// group "1.1" / "2.1" / "2.2": {ok:true, rating_kpa, flange_class, group}
// or {ok:false, reason, hint}. Classes 900/1500/2500 are the class-600 row
// scaled by class/600 (B16.5 ratings at and above class 300 are
// proportional to the class designation).
ojson b16_5_rating(double flange_class, const std::string& group, double temp_c);

}  // namespace chudware::piping
