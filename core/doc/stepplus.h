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
#include <TopoDS_Shape.hxx>

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

// --- studies/<id>/results.npz (durable FEA solve-state cache) -------------------------
// Contract: contracts/stepplus.md §1 `studies/<id>/results.npz`, cap 20 MB/study.
// Mirrors the geometry-cache discipline: engine key + content (input) hash, erase-first
// on save, validate-on-open. Payload is an AICRES1 blob (see sim/sim_cache.h). Larger
// results are dropped on save (re-run to regenerate). Writers that don't understand the
// entry just preserve it (rule §3.1).
//
// entry path: "studies/<study_id>/results.npz"
std::string stepplus_study_result_entry(const std::string& study_id);
// Erase every studies/*/results.* entry from an extra map (erase-first-on-save).
void stepplus_erase_study_results(std::map<std::string, std::string>& extra);
// Encode + put a result (erase-first for that study). Returns false when the payload
// would exceed the 20 MB cap or the governor signals low memory (honest drop).
bool stepplus_put_study_result(std::map<std::string, std::string>& extra,
                               const std::string& study_id, const std::string& kind,
                               const std::string& input_hash, const nlohmann::json& result);
// Validate-on-open: engine key must match. Returns false on miss/corrupt/stale engine.
// Caller compares input_hash_out against the current study's input hash.
bool stepplus_get_study_result(const std::map<std::string, std::string>& extra,
                               const std::string& study_id, std::string& kind_out,
                               std::string& input_hash_out, nlohmann::json& result_out);

// --- solved-bodies BREP cache blob (the `cache.brep` container entry) ---------------
// doc_save embeds the compiled bodies (BREP **with triangulation**, BinTools VERSION_4)
// so an assembly can load this part as a component WITHOUT re-running compile_document
// or re-meshing. Same validity gates as cache.aicm (engine key + doc hash) plus an
// explicit `solved: true` flag in the cache.json header — the flag is only ever written
// after a fully-ok rebuild and the whole container is written atomically (tmp+rename),
// so a crash mid-rebuild can never persist a half-built result.
// Layout: "AICBREP1" magic, u64 body count, then per body u64 length + BinTools stream.
std::string brep_cache_encode(const std::vector<TopoDS_Shape>& bodies);
// Throws OpError("io_error", ...) on a corrupt blob; callers treat that as a cache miss.
std::vector<TopoDS_Shape> brep_cache_decode(const std::string& blob);

}  // namespace aicad::doc
