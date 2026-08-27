#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace chudware::ecad::fab {

// KiCad-style `(export (version "E") (components ...) (nets ...))` s-expression.
std::string write_sch_netlist(const nlohmann::json& sch, const nlohmann::json& netlist);

// fmt: "csv" | "json". Groups by (value, footprint, sorted user fields); natural ref sort.
std::string write_sch_bom(const nlohmann::json& netlist, const std::string& fmt, bool exclude_dnp);

// Ngspice deck built from the netlist + each component's sim mapping.
std::string write_spice_deck(const nlohmann::json& sch, const nlohmann::json& netlist);

}  // namespace chudware::ecad::fab
