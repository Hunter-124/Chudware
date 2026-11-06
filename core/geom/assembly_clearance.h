// Read-only clearance evaluation on world-placed solids.
#pragma once

#include <TopoDS_Shape.hxx>
#include <nlohmann/json.hpp>
#include <string>
#include <vector>

namespace chudware::geom::clearance {
struct Member {
    std::string id;
    std::string name;
    TopoDS_Shape shape;
};

// Malformed requests throw std::invalid_argument. Kernel and missing-geometry
// failures are reported per pair; they never imply clearance.
nlohmann::json removal_report(const std::vector<Member>& members, const nlohmann::json& params);
nlohmann::json envelope_report(const std::vector<Member>& members, const nlohmann::json& params);
}  // namespace chudware::geom::clearance
