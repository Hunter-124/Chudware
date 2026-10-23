// Engineering-metadata supply-rail resolution (subset of
// engine_oracle/ecad/metadata.py used by the firmware pillar).
//
// coverify (decoupling checks) and powerprofile (per-rail currents) both read
// `eng.supply.rails` for a board footprint. The C++ core has no symbol-library
// eng seam, so the starter-library eng blocks are embedded here — probed from
// engine_oracle/ecad/library/starter.py at port time (15 symbols carry
// rails; NO library footprint does, so the footprint-eng layer is always empty).
// Precedence mirrors metadata.resolve_eng: library footprint eng (empty)
// < library symbol eng < per-instance overrides (eng_overrides / fields.eng);
// an overrides block that HAS a supply.rails list replaces the library list
// wholesale (lists replace in deep_merge).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::firmware {

// Normalized rail dicts for a footprint: every rail carries all keys
// (name, voltage_v_min, voltage_v_nom, voltage_v_max, current_a_active,
// current_a_sleep; null when unknown). Empty when neither an instance override
// nor the embedded library table provides rails.
std::vector<nlohmann::json> eng_supply_rails(const nlohmann::json& fp);

}  // namespace chudware::firmware
