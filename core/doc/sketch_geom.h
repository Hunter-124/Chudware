// Sketch trim / extend / offset / fillet / chamfer geometry — C++ port of
// engine/doc/sketch_geom.py. Pure 2D math, no document/op imports: callers hand in
// plain entity dicts (line {p1,p2} | circle {center,radius} | arc {center,radius,
// start,end,ccw}, angles in DEGREES CCW from +x, matching the solver) and get plain
// entity dicts back (no ids — the op layer assigns them).
//
// Only line / circle / arc are supported; anything else throws UnsupportedEntity so the
// op layer can surface a clean unsupported_entity error. TOL = 1e-7 sketch units.
#pragma once

#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::doc::sketchgeom {

// A path entity that is not a line/circle/arc (mirrors sketch_geom.UnsupportedEntity).
struct UnsupportedEntity : std::runtime_error {
    std::string etype;
    explicit UnsupportedEntity(std::string t)
        : std::runtime_error("unsupported entity type '" + t + "'"), etype(std::move(t)) {}
};

// Degenerate fillet/chamfer input (mirrors sketch_geom.GeomError).
struct GeomError : std::runtime_error {
    std::string reason;
    explicit GeomError(std::string r) : std::runtime_error(r), reason(std::move(r)) {}
};

// Split `target` at every in-range interior crossing with `others`, then drop the span
// nearest (px,py). Empty result => no interior crossing => caller deletes the whole entity.
std::vector<nlohmann::json> split_entity(const nlohmann::json& target,
                                         const std::vector<nlohmann::json>& others,
                                         double px, double py);

// Detailed split: the surviving spans PLUS the doomed span that was removed (so the caller
// can re-emit it as construction geometry). `whole` => no interior crossing, the entire
// entity is doomed and `removed` holds a copy of `target`.
struct SplitResult {
    std::vector<nlohmann::json> survivors;
    std::optional<nlohmann::json> removed;
    bool whole = false;
};
SplitResult split_entity_ex(const nlohmann::json& target,
                            const std::vector<nlohmann::json>& others,
                            double px, double py);

// Extend the "start"|"end" of `target` (line/arc) to the first crossing ahead. nullopt
// => nothing ahead (caller raises extend_no_target). circle => UnsupportedEntity.
std::optional<nlohmann::json> extend_entity(const nlohmann::json& target,
                                            const std::string& end,
                                            const std::vector<nlohmann::json>& others);

// Offset a connected chain of lines/arcs by `distance` on the side of (sx,sy).
std::vector<nlohmann::json> offset_chain(const std::vector<nlohmann::json>& entities,
                                         double distance, double sx, double sy);

// Is the chain closed (first start ~= last end)?
bool chain_is_closed(const std::vector<nlohmann::json>& entities);

// Fillet two lines with an arc of `radius`. Returns {ta,tb,arc,end_a,end_b}. GeomError
// on degenerate input (parallel / no corner / radius too large).
nlohmann::json fillet_corner(const nlohmann::json& la, const nlohmann::json& lb, double radius);

// Chamfer two lines: trim each back by its setback. Returns {ta,tb,end_a,end_b}.
nlohmann::json chamfer_corner(const nlohmann::json& la, const nlohmann::json& lb,
                              double dist_a, double dist_b);

}  // namespace chudware::doc::sketchgeom
