// .step+ container reader/writer — C++ port of engine/doc/stepplus.py
// (docs/contracts/stepplus.md).
//
// A `.step+` is a ZIP container that travels like a STEP file: any CAD opens the
// embedded `geometry.step`; AI-CAD gets the full parametric document back. This
// port covers the PARAMETRIC round-trip (the load-bearing path):
//   save(doc, path, extra)  -> writes thumbnail.png (STORED), meta.json,
//                              document.json + any preserved unknown entries. Atomic.
//   open(path)              -> {doc, warnings, extra_entries}.
//
// compile_document is deterministic, so a JSON-only round-trip is FAITHFUL: open =
// unzip + read document.json + rebuild. geometry.step (the frozen last-good rebuild,
// for interchange + total-rebuild-failure fallback) is DEFERRED to a later increment
// (it needs an OCCT STEPControl_Writer); plain open() here never produces it and
// tolerates its absence, exactly like the Python oracle.
#pragma once

#include <map>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace aicad::doc {

// The result of opening a .step+ container.
struct StepPlusDoc {
    nlohmann::json doc;                            // the parsed document.json tree
    std::vector<std::string> warnings;             // non-fatal load notes
    std::map<std::string, std::string> extra;      // unknown entries (name -> raw bytes)
};

// Write `doc` to `path` as a .step+ container. `extra` carries unknown entries to
// preserve verbatim (forward-compat). Atomic: writes "<path>.tmp" then replaces.
// Returns the final path. Throws OpError("io_error", ...) on any failure.
std::string stepplus_save(const nlohmann::json& doc, const std::string& path,
                          const std::map<std::string, std::string>& extra);

// Read a .step+ container at `path`. Validates the container version (major == 1).
// Throws OpError("io_error", ...) if the file is missing/corrupt or not a .step+ .
StepPlusDoc stepplus_open(const std::string& path);

}  // namespace aicad::doc
