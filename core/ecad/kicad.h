// KiCad schematic/board and symbol-library interchange.
//
// S-expressions are parsed into nlohmann::json trees (list -> array, bare
// symbol/quoted string -> string, int -> integer, float -> double). Importers
// produce aicad.sch/1 and aicad.pcb/1 documents. Schematic export targets
// KiCad 7 syntax. KiCad uses +y-down; document coordinates use +y-up.
//
// Errors: malformed input throws std::runtime_error. The session layer wraps
// importer failures in its normal OpError envelope.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace chudware::ecad::kicad {

// engine/ecad/library/sexpr.py::parse — parse full S-expression text into a
// nested json tree. Throws std::runtime_error on malformed input.
nlohmann::json sexpr_parse(const std::string& text);

// engine/ecad/formats.py::import_kicad_sch — *source* is a filesystem path or
// raw S-expression text (raw when it starts with '('). Returns an aicad.sch/1
// document. Throws std::runtime_error on read/parse failure.
nlohmann::json import_kicad_sch(const std::string& source);

// Serialize a schematic document to KiCad 7 .kicad_sch S-expression text.
std::string export_kicad_sch(const nlohmann::json& sch);

// Parse a .kicad_sym library. Unqualified symbol names remain unqualified
// because the library nickname is not encoded in the file itself.
// Returns {"symbols": {libid: SymbolDef, ...}, "warnings": [...]}.
nlohmann::json parse_kicad_sym_library(const std::string& text);

// engine/ecad/formats.py::import_kicad_pcb — same contract, aicad.pcb/1 result.
nlohmann::json import_kicad_pcb(const std::string& source);

}  // namespace chudware::ecad::kicad
