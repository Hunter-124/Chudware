// Editable, nominal geometry for the bundled piping fitting catalog.
#pragma once

#include <string>

#include "parts/parts.h"

namespace chudware::parts {

// Resolve an adapter catalog_id (including its type and secondary size/class)
// against the fitting catalog. Throws std::invalid_argument on malformed or
// unavailable identities; never chooses the catalog's nearest-size hint.
json fitting_catalog_identity(const std::string& size);

// Emit native document ops. Without an explicit options.schedule, schedule 40
// pipe dimensions are assumed and this is recorded in meta.specs. Options may
// contain only schedule; unavailable size/schedule pairs fail explicitly.
PartBuild build_fitting_geometry(const std::string& size);
PartBuild build_fitting_geometry(const std::string& size, const json& options);

}  // namespace chudware::parts
