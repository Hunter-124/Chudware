// .ecad+ container reader/writer — C++ port of engine/ecad/formats.py (the
// save/open half; the KiCad S-expression importers are DEFERRED — the host still
// routes .kicad_sch/.kicad_pcb opens to the Python worker).
//
// A `.ecad+` is a ZIP that travels as a self-contained ECAD document:
//   thumbnail.png  (STORED, written first)   meta.json   document.json
//   netlist.json   (optional, schematic only)   + any preserved unknown entries.
// MIRRORS core/doc/stepplus.{h,cpp} (the .step+ container) — same miniz ZIP I/O,
// same atomic write, same forward-compat unknown-entry preservation.
//
// The PARITY GATE never byte-compares the archive (DEFLATE is non-deterministic);
// it compares the round-tripped document.json + that the OTHER engine can open the
// file (exactly like cpp_session_parity does for .step+).
#pragma once

#include <map>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace aicad::ecad {

// The result of opening a .ecad+ container.
struct EcadContainerDoc {
    nlohmann::json doc;                            // the parsed document.json tree
    std::vector<std::string> warnings;             // non-fatal load notes
    std::map<std::string, std::string> extra;      // unknown entries (name -> raw bytes)
};

// Write `doc` to `path` as a .ecad+ ZIP container. `extra` carries unknown entries
// to preserve verbatim (forward-compat). Atomic: writes "<path>.tmp" then replaces.
// Returns the final path. Throws OpError("io_error", ...) on any failure.
std::string ecad_container_save(const nlohmann::json& doc, const std::string& path,
                                const std::map<std::string, std::string>& extra);

// Read a .ecad+ container at `path`. Validates the container version (major == 1).
// Throws OpError("io_error", ...) if the file is missing/corrupt or not a .ecad+ .
EcadContainerDoc ecad_container_open(const std::string& path);

}  // namespace aicad::ecad
