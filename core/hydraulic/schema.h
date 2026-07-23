// core/hydraulic/schema.h — schema validation + factories for
// aicad.hydraulic/1 documents. C++ port of engine_oracle/hydraulic/schema.py
// (the executable form of contracts/hydraulic_schema.md). Structural
// validation only; error strings carry the same JSON-path-ish locators as the
// oracle, byte-for-byte. Coordinates are [x, y] in mm (schematic canvas);
// pressures in bar; flows in L/min.
#pragma once

#include <string>
#include <vector>

#include "piping/pyutil.h"

namespace aicad::hydraulic {

using pyutil::ojson;

inline const char* SCHEMA_HYD = "aicad.hydraulic/1";

// ISO 1219 component types (sorted, for the "valid types" hint).
const std::vector<std::string>& hyd_component_types_sorted();
bool is_component_type(const std::string& type);

ojson new_hydraulic(const std::string& name = "Hydraulic Circuit");
// Extra params merge into comp["params"] ("angle"/"ports" go top-level,
// mirroring the oracle's **kw absorb).
ojson new_component(const std::string& id, const std::string& type,
                    const std::string& name = "", double x = 0.0, double y = 0.0,
                    const ojson& extra = nullptr);
ojson new_hose(const std::string& id, const std::string& net_name,
               const ojson& connections);

bool is_hydraulic(const ojson& doc);
// Next unused "<prefix><n>" id ("c" -> components, "h" -> hoses).
std::string next_id(const ojson& doc, const std::string& prefix);

std::vector<std::string> validate_component(const ojson& comp,
                                            const std::string& loc = "component");
std::vector<std::string> validate_hose(const ojson& hose,
                                       const std::vector<ojson>& component_ids,
                                       const std::string& loc = "hose");
std::vector<std::string> validate_hydraulic(const ojson& doc);
inline std::vector<std::string> validate(const ojson& doc) { return validate_hydraulic(doc); }

}  // namespace aicad::hydraulic
