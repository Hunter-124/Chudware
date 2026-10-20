// .step+ container reader/writer — C++ port of engine/doc/stepplus.py
// (docs/contracts/stepplus.md).
//
// A `.step+` is a ZIP container that travels like a STEP file: any CAD opens the
// embedded `geometry.step`; Chudware gets the full parametric document back. This
// port covers the PARAMETRIC round-trip (the load-bearing path):
//   save(doc, path, extra)  -> writes thumbnail.png (STORED), meta.json,
//                              document.json + any preserved unknown entries. Atomic.
//   open(path)              -> {doc, warnings, extra_entries}.
// Path arguments and returned path strings are UTF-8 on every platform.
//
// compile_document is deterministic, so a JSON-only round-trip is FAITHFUL: open =
// unzip + read document.json + rebuild. `geometry.step` (the AP214 snapshot of the last
// successful rebuild — interchange for foreign CAD plus the total-rebuild-failure
// fallback) is written by doc_save through stepplus_geometry_step_bytes below; open
// still tolerates its absence (a document that has never rebuilt has none).
#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>
#include <TopoDS_Shape.hxx>
#include "doc/native_policy.h"

namespace chudware::doc {

// The result of opening a .step+ container.
struct StepPlusDoc {
    nlohmann::json doc;                            // the parsed document.json tree
    std::vector<std::string> warnings;             // non-fatal load notes
    std::map<std::string, std::string> extra;      // unknown entries (name -> raw bytes)
    // Geometry caches (cache.json/.aicm/.brep/.ckpt) present in the archive but not
    // admitted: name -> entry_limit | aggregate_limit | compression_ratio | extract_failed.
    // A skipped cache is a cache miss the caller reports; it never fails the open.
    std::map<std::string, std::string> skipped;
};

// Write `doc` to `path` as a .step+ container. `extra` carries unknown entries to
// preserve verbatim (forward-compat). Atomic: private exclusive staging, then replacement.
// Returns the final path. Throws OpError("io_error", ...) on any failure. Optional
// caches (cache.aicm, cache.ckpt) that do not fit the archive budget left by every
// other entry are not written; their names are appended to `dropped` when given.
std::string stepplus_save(const nlohmann::json& doc, const std::string& path,
                          const std::map<std::string, std::string>& extra,
                          std::vector<std::string>* dropped = nullptr);
// Expanded bytes stepplus_save(doc, ..., extra) leaves for the optional caches once
// every other entry of `extra` (and document/meta/thumbnail) is reserved. The writer
// admits cache.aicm first, then cache.ckpt, against this same remainder.
std::size_t stepplus_optional_cache_budget(const nlohmann::json& doc,
                                           const std::map<std::string, std::string>& extra);

// Read a .step+ container at `path`. Validates the container version (major == 1).
// Throws OpError("io_error", ...) if the file is missing/corrupt or not a .step+ .
StepPlusDoc stepplus_open(const std::string& path);

// Read-only component/dependency consumers never resave the returned payload.
// These retain normal-open archive admission checks but skip unrelated payload I/O.
// Document reads return only document.json; component reads additionally return the
// solved cache.json/cache.brep entries for parts (assemblies rebuild descendants).
// Use stepplus_open for editable sessions so unknown entries survive a later save.
nlohmann::json stepplus_read_document(const std::string& path);
StepPlusDoc stepplus_open_component(const std::string& path);
// Shared editable-container machinery used by .step+ and standalone .draw+.
// `container` is the exact meta.json identity (for example "aicad.draw+/1");
// `kind` is the meta.json document kind. `expected_prefix` includes the trailing
// slash (for example "aicad.draw+/"). Unknown entries are preserved verbatim.
std::string editable_container_save(const nlohmann::json& doc, const std::string& path,
                                    const std::map<std::string, std::string>& extra,
                                    const std::string& container, const std::string& kind,
                                    const std::string& format_name);
StepPlusDoc editable_container_open(const std::string& path,
                                    const std::string& expected_prefix,
                                    const std::string& format_name);
// Canonical document.json bytes used by every editable container and nested source.
std::string editable_document_json(const nlohmann::json& doc);

// Metadata-only inspection and copy-only relocation of native files. Never compiles
// geometry or registers a session. Relocation preserves authored data and invalidates
// optional display/BREP caches; destinations must not exist.
nlohmann::json native_file_operations(const nlohmann::json& params);


// --- persisted-geometry cache keys (fast cold-open) --------------------------------
// doc_save may embed the compiled tessellation + ops-response as extra `cache.aicm` /
// `cache.json` entries, the solved bodies as `cache.brep` and the per-feature build
// checkpoints as `cache.ckpt`, so a later doc_open returns the display AND a warm
// session WITHOUT re-running compile_document; the first edit after reopen executes
// only the features after the deepest restored checkpoint. The caches never touch
// document.json (the parametric source of truth). Validity is gated on TWO stable keys:

// A run-stable 64-bit FNV-1a hash of the CANONICAL document json (same canonicalization
// stepplus_save writes), so open can prove the geometry matches the exact saved tree. Not
// std::hash (which is not guaranteed stable across runs/platforms — the value is persisted).
std::string stepplus_doc_hash(const nlohmann::json& doc);

// An engine/kernel identity string (OCCT version + a cache-format schema constant). Any
// change to the geometry compiler that could alter output MUST bump the schema constant so
// stale caches from older engines are rejected. Mismatch on open -> full rebuild.
std::string stepplus_cache_engine_key();

// Cache authority is process/cell state, never document-controlled.
void configure_native_policy(const std::string& root, const std::string& state_dir);
// Component loads authenticate the signed manifest and only hash the BREP/header
// members they consume, without allocating unrelated mesh/checkpoint payloads.
bool stepplus_cache_verified(const nlohmann::json& doc,
                            const std::map<std::string, std::string>& entries,
                            bool component_only = false);

// --- build-state write-back (contracts/stepplus.md §17) -------------------------------
// Identity of a container file on disk: size + modification time (nanoseconds since the
// filesystem clock epoch). `valid` is false when the path is not a regular file.
struct NativeFileStamp {
    std::uintmax_t size = 0;
    long long mtime_ns = 0;
    bool valid = false;
    bool operator==(const NativeFileStamp& o) const {
        return valid && o.valid && size == o.size && mtime_ns == o.mtime_ns;
    }
};
NativeFileStamp stepplus_file_stamp(const std::string& path);

struct BuildStateWriteback {
    bool written = false;
    std::string reason;                 // why nothing was written (empty when written)
    std::vector<std::string> entries;   // build-state members now in the file
    std::vector<std::string> dropped;   // optional caches left out by the archive budget
};
// Replace ONLY the derived build-state members (cache.json/.aicm/.brep/.ckpt and
// geometry.step) of the .step+ at `path` with `entries`. Every other member —
// document.json, meta.json, thumbnail.png, drawings, studies, unknown entries — is copied
// as its compressed stream, byte for byte, so the document and its metadata stay exactly
// as authored. Refuses (written=false, reason) unless the file still carries `expected`
// and its document.json hashes (stepplus_doc_hash) to `doc_hash`: `file_changed`,
// `document_changed`, `not_a_container`, `io_error: ...`. Atomic (private temp + rename);
// never throws.
BuildStateWriteback stepplus_write_build_state(const std::string& path,
                                               const NativeFileStamp& expected,
                                               const std::string& doc_hash,
                                               const std::map<std::string, std::string>& entries);

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

// --- geometry.step (contracts/stepplus.md §1) ---------------------------------------
// AP214 (AUTOMOTIVE_DESIGN, millimetre) STEP bytes for `bodies` — the container member any
// other CAD opens, and the frozen fallback contracts/stepplus.md §2.3 offers as an `import`
// feature when a rebuild fails. doc_save calls this after a fully-ok rebuild with the part's
// compiled bodies, or an assembly's FLATTENED world-placed component bodies, and stores the
// result as the `geometry.step` entry. Each solid is transferred on its own (an AsIs compound
// transfer of disjoint lumps emits one malformed manifold_solid_brep — see export_bodies);
// a body with no solid (open sheet/shell) is transferred as-is so surfaces still travel.
// Returns "" when there is nothing to write or the OCCT writer refuses — an honest drop, so
// a writer problem can never fail the save (the entry is then simply absent).
std::string stepplus_geometry_step_bytes(const std::vector<TopoDS_Shape>& bodies);

}  // namespace chudware::doc
