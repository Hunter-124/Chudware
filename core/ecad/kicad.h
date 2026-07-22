// KiCad importers — C++ port of engine/ecad/library/{sexpr,kicad_sym,kicad_mod}.py
// and the engine/ecad/formats.py::import_kicad_sch / import_kicad_pcb importers.
//
// S-expressions are parsed into nlohmann::json trees (list -> array, bare
// symbol/quoted string -> string, int -> integer, float -> double), mirroring
// the Python parser's atom typing. The importers produce aicad.sch/1 and
// aicad.pcb/1 documents that pass schema::validate_schematic / validate_board.
// Coordinate convention (same as the oracle): KiCad uses +y-down; all y coords
// are negated to +y-up. Angles pass through unchanged.
//
// Errors: malformed input throws std::runtime_error carrying the Python
// ValueError message; the session layer (ecad_open) wraps it into the same
// OpError("io_error", "import_kicad_* failed for '<path>': <exc>") envelope the
// Python rpc.py raises.
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace aicad::ecad::kicad {

// engine/ecad/library/sexpr.py::parse — parse full S-expression text into a
// nested json tree. Throws std::runtime_error on malformed input.
nlohmann::json sexpr_parse(const std::string& text);

// engine/ecad/formats.py::import_kicad_sch — *source* is a filesystem path or
// raw S-expression text (raw when it starts with '('). Returns an aicad.sch/1
// document. Throws std::runtime_error on read/parse failure.
nlohmann::json import_kicad_sch(const std::string& source);

// engine/ecad/formats.py::import_kicad_pcb — same contract, aicad.pcb/1 result.
nlohmann::json import_kicad_pcb(const std::string& source);

}  // namespace aicad::ecad::kicad
