// OMP session recovery actions and per-conversation session links. The link is metadata,
// never a second transcript.
//
// Deliberately GL/ImGui/AppState-free so host/tests/test_omp_session_actions.cpp links
// the REAL implementation rather than a copy of it (same pattern as omp_descriptors.h).
// Parsers accept both the worker's snake_case spellings and the Omp-native camelCase
// passthrough, but never invent data: absent fields stay absent, and a refusal envelope is
// reported as a refusal, not silently coerced into an empty success.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace ui {

// Worker method names (contract-frozen; the backend slice serves exactly these).
inline constexpr char kOmpExportSessionMethod[] = "omp_export_session";
inline constexpr char kOmpBranchSessionMethod[] = "omp_branch_session";
inline constexpr char kOmpCloseSessionMethod[]  = "omp_close_session";

// ---- per-conversation Omp session link ---------------------------------------------------
// What the copilot knows about the Omp runtime session behind one Chudware conversation.
// sessionId/sessionFile/health persist (conversation JSON, "omp" key); runtimeLive is
// process-local truth ("this app process launched/reattached the service-side runtime") and is
// deliberately NEVER persisted — after a restart every session is inactive until its next turn
// resumes it via omp_session_file.
struct OmpSessionLink {
    std::string sessionId;    // desktop runtime id; web opaque cell session handle
    std::string sessionFile;  // desktop-only runtime resume file (never sent by web)
    std::string health;       // historical metadata retained during migration, not a live verdict
    bool runtimeLive = false; // process-local; not serialized
    bool empty() const { return sessionId.empty() && sessionFile.empty() && health.empty(); }
};

// Serialize the persisted fields ({"session_id","session_file","health"}; empty fields
// omitted). Returns a JSON null for an empty link so callers can skip the key entirely.
nlohmann::json ompSessionLinkToJson(const OmpSessionLink& link);
// Parse a persisted link (object under the conversation's "omp" key). Returns false
// (out untouched) when the value carries nothing usable.
bool ompSessionLinkFromJson(const nlohmann::json& j, OmpSessionLink& out);

// Registry keyed by Chudware conversation id. GL/UI-thread only (matching every other
// copilot-resident store); the tests drive it single-threaded.
const OmpSessionLink* ompSessionLink(const std::string& convId);
OmpSessionLink&       ompSessionLinkEnsure(const std::string& convId);
void ompSessionLinkSet(const std::string& convId, OmpSessionLink link);
void ompSessionLinkErase(const std::string& convId);
// persistChatHistory re-keys a duplicate session id in place — the link follows the
// transcript it describes. No-op when oldId has no link; an existing newId link is replaced.
void ompSessionLinkRekey(const std::string& oldId, const std::string& newId);
// loadChatHistory rebuilds the resident set from disk — the registry rebuilds with it.
void ompSessionLinksClear();

// Conversation-JSON persistence seam (the exact calls conversations.cpp makes, so the tests
// exercise the shipped behavior):
//   persist — writes convObj["omp"] from the registry; touches NOTHING else in convObj
//             (the transcript is Chudware's own store — the link never duplicates it).
//   restore — reads convObj["omp"] into the registry for convId; absent/empty key
//             leaves the registry without an entry.
void ompLinkPersist(const std::string& convId, nlohmann::json& convObj);
void ompLinkRestore(const std::string& convId, const nlohmann::json& convObj);

// ---- request params ---------------------------------------------------------------------------
nlohmann::json ompExportSessionParams(const std::string& sessionId,
                                           const std::string& outputPath = std::string());
nlohmann::json ompBranchSessionParams(const std::string& sessionId,
                                           const std::string& entryId);
nlohmann::json ompCloseSessionParams(const std::string& sessionId);

// ---- response envelopes -----------------------------------------------------------------------
// Human-readable failure text from a worker refusal ({ok:false, reason|error}) or a
// non-object response; `fallback` when the envelope carries no text of its own.
std::string ompActionError(const nlohmann::json& r, const char* fallback);

// True when the response says the method itself is absent (older worker / slice not
// deployed) — callers latch the whole surface off instead of retrying forever. Session-surface
// variant of core::workerMethodAbsent (async_rpc.h), expressed over primitives so this TU stays
// AppState-free; it matches transport text more loosely than that strict helper does.
bool ompSessionRpcAbsent(bool transportOk, const std::string& transportErr,
                              const nlohmann::json& result);

// omp_export_session -> {ok, path}. False when not an ok envelope or path missing.
struct OmpExportResult {
    std::string path;
};
bool parseOmpExportResult(const nlohmann::json& r, OmpExportResult& out);

// True for the worker's clean "session_not_live" refusal (export/branch on an
// inactive runtime). Callers drop runtimeLive and show the resume hint instead of an error.
bool ompSessionNotLive(const nlohmann::json& r);

// omp_branch_session -> {ok, omp_session_id, omp_session_file}.
// Desktop requires both resume fields; web requires only the opaque cell handle.
struct OmpBranchResult {
    std::string sessionId;
    std::string sessionFile;
};
bool parseOmpBranchResult(const nlohmann::json& r, OmpBranchResult& out);

// omp_close_session -> {ok} (idempotent: closing an already-closed runtime is ok:true).
bool parseOmpCloseResult(const nlohmann::json& r);


}  // namespace ui
