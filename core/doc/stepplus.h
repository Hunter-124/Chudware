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

// --- persisted-geometry cache keys (fast cold-open) --------------------------------
// doc_save may embed the compiled tessellation + ops-response as extra `cache.aicm` /
// `cache.json` entries so a later doc_open can return them WITHOUT re-running
// compile_document (rebuild-on-open is the dominant cold-open cost). The cache is
// display-only: it never touches document.json (the parametric source of truth), and the
// first edit recompiles from features. Validity is gated on TWO stable keys:

// A run-stable 64-bit FNV-1a hash of the CANONICAL document json (same canonicalization
// stepplus_save writes), so open can prove the geometry matches the exact saved tree. Not
// std::hash (which is not guaranteed stable across runs/platforms — the value is persisted).
std::string stepplus_doc_hash(const nlohmann::json& doc);

// An engine/kernel identity string (OCCT version + a cache-format schema constant). Any
// change to the geometry compiler that could alter output MUST bump the schema constant so
// stale caches from older engines are rejected. Mismatch on open -> full rebuild.
std::string stepplus_cache_engine_key();

}  // namespace aicad::doc
