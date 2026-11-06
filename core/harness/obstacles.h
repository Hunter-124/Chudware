// core/harness/obstacles.h — the obstacle set a cable path is routed / checked against:
// the document's tessellated solids (minus ignore_bodies) plus caller keepouts, each owner
// with its own clearance. Owner thresholds are CENTERLINE distances: clearance + od/2.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "harness/geometry.h"

namespace chudware::harness {

struct ObstacleOwner {
    int body_id = 0;  // document body id; 0 for an AABB keepout
    std::string name;
    bool keepout = false;
    double clearance_mm = 0.0;  // wire-surface gap required to this owner
};

struct ObstacleSet {
    TriBvh bvh;
    std::vector<ObstacleOwner> owners;
    std::vector<double> thr;  // centerline distance threshold per owner: clearance + od/2
    double thr_max = 0.0;
    double thr_min = 0.0;
    bool has_geometry = false;
    V3 lo, hi;  // bounding box of all obstacle triangles (valid iff has_geometry)
    size_t triangle_count = 0;
    std::vector<std::string> ignored;   // ignore_bodies entries, as given
    std::vector<std::string> warnings;

    // "name (#id)" / "keepout:name" for messages.
    std::string label(int owner) const;
    int owner_count() const { return static_cast<int>(owners.size()); }
};

// Builds the set from `scene` and params {ignore_bodies?:[id|name], keepouts?:[{aabb:[[x,y,z],
// [x,y,z]] | body:id|name, clearance_mm?}]}. `od_mm` is the cable outer diameter,
// `default_clearance_mm` the wire-surface gap required to every body without its own
// override. Returns "" on success, else a bad-arguments message.
std::string build_obstacles(const nlohmann::json& params, const Scene& scene, double od_mm,
                            double default_clearance_mm, ObstacleSet& out);

}  // namespace chudware::harness
