// Durable simulation solve-state cache — content-addressed results for FEA /
// SPICE / CFD (and a reserved EM kind). Process-wide + optional on-disk sidecar;
// the .step+ studies/<id>/results.npz seam lives in doc/stepplus (20 MB cap).
//
// Keys are FNV-1a of a CANONICAL dump of every solve input. A HIT returns the
// stored result (O(load)); ANY input change is a MISS. Never serves a stale
// result. Respects resource_governor::low_memory_now() (honest disable).
#pragma once

#include <map>
#include <cstdint>
#include <string>

#include <nlohmann/json.hpp>

namespace aicad::sim::cache {

// Contract cap for a single studies/<id>/results.* payload (stepplus.md §1).
constexpr std::size_t kMaxStudyResultBytes = 20ull * 1024ull * 1024ull;

enum class Kind {
    FeaStatic = 0,
    FeaModal,
    FeaThermal,
    Spice,
    Cfd,
    Em,
};

const char* kind_name(Kind k);

// Run-stable FNV-1a hex of a canonical JSON dump of `inputs` (floats rounded to
// 6 dp; keys sorted by nlohmann::json). For FEA studies that carry a step_path,
// prefer hash_fea_study so the STEP *contents* (not the path string) fold in.
std::string hash_inputs(Kind kind, const nlohmann::json& inputs);

// FEA study hash: canonical study JSON with step_path replaced by a content
// fingerprint of the file (or the path string when the file is unreadable).
// `kind` must be FeaStatic / FeaModal / FeaThermal so the three analyses never collide.
std::string hash_fea_study(Kind kind, const nlohmann::json& study);

// SPICE: circuit + study (analyses/probes/options). CFD: geometry + FlowParams.
std::string hash_spice(const nlohmann::json& circuit, const nlohmann::json& study);
std::string hash_cfd(const nlohmann::json& geom_and_params);

struct Lookup {
    bool hit = false;
    nlohmann::json result;
    // "hit" | "miss" | "low_memory" | "disabled" | "corrupt" | "stale"
    std::string reason = "miss";
    std::string input_hash;
};

// Process-wide lookup/store. On low_memory_now() lookup is always a miss and
// store is a no-op (honest). store refuses payloads larger than the 20 MB cap.
Lookup lookup(Kind kind, const std::string& input_hash);
bool store(Kind kind, const std::string& input_hash, const nlohmann::json& result);

// Optional disk sidecar (under set_sidecar_dir, or $AICAD_SIM_CACHE_DIR, or a
// process temp dir). Same keying; used so SPICE/EM/CFD results survive the
// process without a .step+ container. Disabled when low_memory_now().
void set_sidecar_dir(const std::string& dir);
std::string sidecar_dir();

// Test / diagnostics.
void clear();
struct Stats {
    std::uint64_t hits = 0;
    std::uint64_t misses = 0;
    std::uint64_t stores = 0;
    std::uint64_t drops = 0;  // refused (too large / low mem / disabled)
};
Stats stats();
void reset_stats();

// Engine key for result-blob headers (mirrors stepplus_cache_engine_key discipline).
// Bump schema when the result JSON shape or solver semantics change.
std::string sim_cache_engine_key();

// Encode/decode a study result blob for studies/<id>/results.npz:
//   "AICRES1" magic · u32 header_len · header JSON · result JSON
// Header: {engine, input_hash, kind, solved:true, bytes}.
// Returns empty string when the payload would exceed kMaxStudyResultBytes or
// when low_memory_now() is set (honest drop).
std::string encode_result_blob(Kind kind, const std::string& input_hash,
                               const nlohmann::json& result);
// Validates engine key + solved flag. Returns false on any mismatch/corrupt
// (caller treats as cache miss). Does NOT re-check input_hash equality — the
// caller supplies the expected hash and compares.
bool decode_result_blob(const std::string& blob, Kind& kind_out,
                        std::string& input_hash_out, nlohmann::json& result_out);

// .step+ extra-map helpers (studies/<id>/results.npz). Erase-first on save.
std::string study_result_entry(const std::string& study_id);
void erase_study_results(std::map<std::string, std::string>& extra);
// Put (erase-first for that study id). Drops silently when encode returns empty.
bool put_study_result(std::map<std::string, std::string>& extra,
                      const std::string& study_id, Kind kind,
                      const std::string& input_hash, const nlohmann::json& result);
// Validate-on-open: engine key must match; input_hash returned for caller check.
bool get_study_result(const std::map<std::string, std::string>& extra,
                      const std::string& study_id, Kind& kind_out,
                      std::string& input_hash_out, nlohmann::json& result_out);

}  // namespace aicad::sim::cache
