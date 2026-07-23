// core/piping/schema.h — schema validation + factories for aicad.piping/1
// documents. C++ port of engine_oracle/piping/schema.py (the executable form
// of contracts/piping_schema.md): structural validation only — error strings
// carry the same JSON-path-ish locators as the oracle, byte-for-byte.
// Coordinates are [x, y, z] in mm; angles in degrees.
#pragma once

#include <string>
#include <vector>

#include "piping/pyutil.h"

namespace aicad::piping {

using pyutil::ojson;

inline const char* SCHEMA_PIPING = "aicad.piping/1";

// Factories (mirroring the oracle's new_* helpers).
ojson new_piping(const std::string& name = "Piping");
ojson new_pipe_run(const std::string& id, const ojson& path, const ojson& nps,
                   const std::string& schedule = "40", const std::string& name = "Run",
                   const ojson& bore_mm = nullptr);
ojson new_fitting(const std::string& id, const std::string& type, const ojson& nps,
                  const ojson& origin = nullptr, const std::string& schedule = "40",
                  const std::string& name = "Fitting", const ojson& params = nullptr);

bool is_piping(const ojson& doc);
// Next unused "<prefix><n>" id ("r" -> runs, "ft" -> fittings).
std::string next_id(const ojson& doc, const std::string& prefix);

// Validators — each returns a list of error strings (empty = valid).
std::vector<std::string> validate_run(const ojson& run, const std::string& loc = "run");
std::vector<std::string> validate_fitting(const ojson& ft,
                                          const std::string& loc = "fitting");
std::vector<std::string> validate_piping(const ojson& doc);
inline std::vector<std::string> validate(const ojson& doc) { return validate_piping(doc); }

}  // namespace aicad::piping
