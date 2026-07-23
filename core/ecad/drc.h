// Design Rule Check (DRC) + ratsnest — C++ port of engine/ecad/drc.py.
//
// Pure, advisory geometry audit over a board document (aicad.pcb/1). run_drc()
// returns a deterministic, sorted list of violation dicts; ratsnest() returns the
// still-needed airwires. Mirrors the Python oracle exactly: shape abstraction
// (thick segment / circle / box), net-aware clearance, rule catalog + ordering,
// and the violation record shape {rule, severity, locs, items, message}.
// OCCT-free; pure JSON/geometry.
#pragma once

#include <set>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::ecad {

// A normalized copper item for clearance math (drc.py::_Shape).
struct Shape {
    std::string kind;                 // "seg" | "circle" | "box"
    nlohmann::json net;               // net name (string) or null
    std::set<std::string> layers;     // copper layers occupied
    std::string item_id;
    std::pair<double, double> p1{0, 0};   // seg endpoints
    std::pair<double, double> p2{0, 0};
    double margin = 0.0;              // half-width (seg) / radius (circle)
    std::pair<double, double> center{0, 0};  // circle / box centre
    std::pair<double, double> half{0, 0};    // box half-extents
    std::pair<double, double> loc{0, 0};     // representative location for the violation
};

// All clearance-bearing copper shapes (pads, tracks, vias), sorted by item_id.
std::vector<Shape> copper_shapes(const nlohmann::json& board);

// net helpers
bool same_net(const nlohmann::json& a, const nlohmann::json& b);

// run_drc(board) -> deterministic sorted list of violations. Never raises.
nlohmann::json run_drc(const nlohmann::json& board);

// ratsnest(board) -> deterministic list of {net, p1, p2} airwires.
nlohmann::json ratsnest(const nlohmann::json& board);

}  // namespace chudware::ecad
