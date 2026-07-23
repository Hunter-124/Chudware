// core/piping/catalog.h — pipe + fitting dimensional catalog.
// C++ port of engine_oracle/piping/catalog.py: nearest_pipe resolves
// (nps, schedule) / outer_dia against the vendored ASME B36.10M/B36.19M
// schedule tables (pipe_tables.h, generated from the MIT `fluids` package —
// what the oracle calls at runtime), and fitting_dims reads the bundled
// B16.9/B16.5 fitting CSV (fittings_data.h). All dimensions in mm.
// Every lookup degrades contract-shaped ({"ok": false, "reason", "hint"})
// instead of throwing for a bad size.
#pragma once

#include <string>
#include <vector>

#include "piping/pyutil.h"

namespace chudware::piping {

using pyutil::ojson;

// Success: {"ok": true, "nps", "outer_dia", "inner_dia", "wall", "schedule"}
// (the PipeSpec.as_dict() shape). Failure: {"ok": false, "reason", "hint"}.
ojson nearest_pipe(const ojson& nps = nullptr, const std::string& schedule = "40",
                   const ojson& outer_dia = nullptr);

// Bundled B16 fitting dimensions (mm) for (ftype, nps[, nps2/flange_class]):
// the matching CSV row as an object, or a contract-shaped failure.
ojson fitting_dims(const std::string& ftype, double nps, const ojson& nps2 = nullptr,
                   const ojson& flange_class = nullptr);

// Sorted NPS list catalogued for ftype (empty if none).
std::vector<double> available_sizes(const std::string& ftype);

}  // namespace chudware::piping
