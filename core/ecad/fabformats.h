// Fabrication-output writers — C++ port of engine/ecad/fab/{gerber,excellon,bom,
// placement}.py for aicad.pcb/1 boards.
//
// Produces the EXACT same byte stream as the Python oracle:
//   * RS-274X Gerber  (%FSLAX46Y46*% / %MOMM*%, %ADD D-codes from D10, G36/G37
//     region pours, 4.6 integer coordinates) — write_gerber_layer / write_gerbers
//   * Excellon drill  (M48 / METRIC,TZ header, T## tool table, 3.3 hits, G85
//     slots, M30 end) split into PTH / NPTH — write_drill
//   * BOM CSV / JSON   (Refs,Qty,Value,Footprint,MPN + user fields; DNP excluded;
//     natural ref sort; QUOTE_MINIMAL csv) — write_bom
//   * Pick-and-place CSV (Designator,MidX,MidY,Layer,Rotation) — write_placement
//
// Aperture numbering/ordering, coordinate digit counts, D-code sequencing and
// float formatting all match the oracle exactly (gated by tools/cpp_ecad_parity.py).
#pragma once

#include <map>
#include <string>

#include <nlohmann/json.hpp>

namespace aicad::ecad::fab {

// One Gerber layer as an RS-274X string (trailing '\n').
std::string write_gerber_layer(const nlohmann::json& board, const std::string& layer_id);

// All populated layers -> {layer_id: gerber-text}. (The Python writer returns
// {layer_id: path}; the harness compares CONTENT, so we return text keyed by the
// same layer_id ordering the oracle emits.)
std::map<std::string, std::string> write_gerbers(const nlohmann::json& board);

// Excellon drill files -> {"pth": text, "npth": text}.
std::map<std::string, std::string> write_drill(const nlohmann::json& board);

// BOM as csv (default) or json text.
std::string write_bom(const nlohmann::json& board, const std::string& fmt = "csv",
                      bool exclude_dnp = true);

// Pick-and-place CSV text.
std::string write_placement(const nlohmann::json& board, bool exclude_tht = true,
                            bool exclude_virtual = true);

}  // namespace aicad::ecad::fab
