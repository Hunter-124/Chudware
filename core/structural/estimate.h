// Structural cost takeoff — C++ port of
// engine_oracle/structural/estimate.py: board-feet, steel weight, fastener
// count, and the line-item estimate against the ENR Q4-2025 rate table.
//
// All costs are preliminary budget figures from a rate table — NOT vendor
// prices (bom_price owns those). The rate table is user-overridable: an
// explicit rates_path / $CHUDWARE_STRUCTURAL_RATES JSON file is merged over the
// embedded defaults (core/structural/enr_rates.json is the shipped copy).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "structural/model.h"

namespace chudware::structural {

extern const char* kEstimateDisclaimer;  // byte-exact DISCLAIMER

struct RateTable {
    double cedar_lumber_per_bf = 4.50;
    double steel_per_lb = 1.20;
    double concrete_per_cy = 145.0;
    double labor_per_hr = 85.0;
    double fastener_per_ea = 0.35;
    // install-hours per element type (fallback 0.5 for unlisted types)
    std::vector<std::pair<std::string, double>> install_hours = {
        {"post", 1.0}, {"column", 1.0}, {"beam", 0.75},
        {"joist", 0.25}, {"stud_wall", 0.25}, {"footing", 1.5},
    };

    double install_hour(const std::string& element_type) const;
};

// Load the rate table: explicit path → $CHUDWARE_STRUCTURAL_RATES → embedded
// defaults. Invalid requested overrides fail; valid files merge per key.
RateTable load_rates(const std::string& rates_path = "");

// Quantity helpers validate positive finite member geometry and recognized
// section grammars.
double board_feet(const StructuralElement& el);
double steel_weight_lbs(const StructuralElement& el);
int fastener_count(const StructureDoc& doc);

// Full quantity/rate estimate. The compatible totals remain, with additive
// completeness and per-line material-estimation diagnostics so partial cost
// coverage cannot look final.
nlohmann::json estimate(const StructureDoc& doc, const RateTable& rates);

}  // namespace chudware::structural
