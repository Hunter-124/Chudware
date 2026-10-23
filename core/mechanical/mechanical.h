// core/mechanical/mechanical.h — AGMA spur-gear tooth stress + ISO 281 bearing life
// (C++ port of engine_oracle/mechanical/{gear_check,bearing_life}.py).
// Clean-room re-implementation of equations published in open textbooks
// (Shigley's Mechanical Engineering Design, 10th ed., Ch. 14 / Ch. 11) — not a copy
// of AGMA 2001-D04 or ISO 281:2007. PURE: no OCCT, no doc/agent linkage. Envelopes
// byte-identical to services/mcp/mechanical_tools.py.
#pragma once

#include <stdexcept>
#include <string>

#include <nlohmann/json.hpp>

namespace chudware {
namespace mechanical {

using nlohmann::json;

// Thrown with the exact message of the Python oracle's ValueError.
struct MechError : std::runtime_error { using std::runtime_error::runtime_error; };

// Shared disclaimer (gear_check.py is the single source; byte-exact).
extern const char* const kDisclaimer;

// gear_check: AGMA bending (Shigley Eq. 14-15) + contact (Eq. 14-16) stress, SI
// (mm, N, MPa). Required keys: Wt, module, face_width, pitch_diameter, J, I.
// Optional: Ko/Kv/Ks/Km/KB/Cf (default 1.0), Cp (default 191.0),
// allowable_bending_stress / allowable_contact_stress (null/absent -> SF null).
// Returns {"sigma_bending_MPa","sigma_contact_MPa","SF_bending","SF_contact",
// "disclaimer"} — raw doubles, unrounded, exactly like GearCheckResult.to_dict().
// Throws MechError on non-positive geometry/factors/load.
json gear_check(const json& params);

// bearing_life: ISO 281 L10 = (C/P)^p million revs; L10h = 1e6/(60*n)*L10.
// Required: C, Fr. Optional: Fa (0), n_rpm (1000), bearing_type ("ball"|"roller"),
// X (1), Y (0), bearing_designation (""). C is NEVER looked up — supply the
// datasheet value. Returns BearingLifeResult.to_dict()'s exact shape.
// Throws MechError on C<=0 / both loads<=0 / unknown type / P<=0 / n_rpm<=0.
json bearing_life(const json& params);

// ---- single dispatch entry ------------------------------------------------------
// Owns "gear_check" and "bearing_life". Returns false when the method is not ours.
// The result envelope is the MCP wrapper's {"ok":true, **to_dict()}; oracle
// ValueErrors land as _err()'s {"ok":false,"error"} (no code). Missing/untyped
// required params land as {"ok":false,"error","code":"invalid_input"}. `err` stays
// empty.
bool call(const std::string& method, const json& params, json& result, std::string& err);

}  // namespace mechanical
}  // namespace chudware
