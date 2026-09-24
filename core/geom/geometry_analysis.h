#pragma once

#include <vector>
#include <nlohmann/json.hpp>
#include <TopoDS_Shape.hxx>

namespace chudware::geom {

// The shape/body-id arrays are in the same order as the display AICM bodies. IDs of
// faces and edges are 1-based TopExp::MapShapes indices, just like tessellate_body.
nlohmann::json geometry_analysis(const std::vector<TopoDS_Shape>& shapes,
                                 const std::vector<int>& body_ids,
                                 const nlohmann::json& options);

} // namespace chudware::geom
