// core/hydraulic/bom.h — BOM sourcing helpers for hydraulic circuits.
// C++ port of engine_oracle/hydraulic/bom.py: VendorPart-compatible sourcing
// lines ({id, type, name, mpn, manufacturer, description, quantity, unit,
// category}) so components can join bom_rollup at merge time.
#pragma once

#include "piping/pyutil.h"

namespace chudware::hydraulic {

using pyutil::ojson;

// Sourcing lines for every component (un-sourced ones keep null fields;
// "suppressed": true components are excluded).
ojson hydraulic_bom_lines(const ojson& doc);

// {"total", "by_type", "missing_mpn", "sourced"} — by_type in first-seen order.
ojson sourcing_summary(const ojson& doc);

}  // namespace chudware::hydraulic
