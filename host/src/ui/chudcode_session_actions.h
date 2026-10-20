// Chudcode session recovery actions — export / branch / diagnose+repair / runtime close —
// as served by the persistent Chudcode worker (services/agent/worker_rpc.py:
// chudcode_export_session / chudcode_branch_session / chudcode_repair_session /
// chudcode_close_session), plus the per-conversation Chudcode session LINK the copilot
// persists inside each session's JSON (id + file + last doctor health — metadata only,
// NEVER a second transcript).
//
// Deliberately GL/ImGui/AppState-free so host/tests/test_chudcode_session_actions.cpp links
// the REAL implementation rather than a copy of it (same pattern as chudcode_descriptors.h).
// Parsers accept both the worker's snake_case spellings and the Chudcode-native camelCase
// passthrough, but never invent data: absent fields stay absent, and a refusal envelope is
// reported as a refusal, not silently coerced into an empty success.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace ui {

// Worker method names (contract-frozen; the backend slice serves exactly these).
inline constexpr char kChudcodeExportSessionMethod[] = "chudcode_export_session";
inline constexpr char kChudcodeBranchSessionMethod[] = "chudcode_branch_session";
inline constexpr char kChudcodeRepairSessionMethod[] = "chudcode_repair_session";
inline constexpr char kChudcodeCloseSessionMethod[]  = "chudcode_close_session";
// Edit-message rewind: branch the conversation's runtime session to just BEFORE one of its
// user messages (the runtime resolves the real entry id; the parent file is never touched).
inline constexpr char kChudcodeRewindSessionMethod[] = "chudcode_rewind_session";

// ---- per-conversation Chudcode session link ---------------------------------------------------
// What the copilot knows about the Chudcode runtime session behind one Chudware conversation.
// sessionId/sessionFile/health persist (conversation JSON, "chudcode" key); runtimeLive is
// process-local truth ("this app process launched/reattached the service-side runtime") and is
// deliberately NEVER persisted — after a restart every session is inactive until its next turn
// resumes it via chudcode_session_file.
struct ChudcodeSessionLink {
    std::string sessionId;    // desktop runtime id; web opaque cell session handle
    std::string sessionFile;  // desktop-only runtime resume file (never sent by web)
    std::string health;       // last doctor status: healthy|issues|repaired|repaired_with_issues
    bool runtimeLive = false; // process-local; not serialized
    bool empty() const { return sessionId.empty() && sessionFile.empty() && health.empty(); }
};

// Serialize the persisted fields ({"session_id","session_file","health"}; empty fields
// omitted). Returns a JSON null for an empty link so callers can skip the key entirely.
nlohmann::json chudcodeSessionLinkToJson(const ChudcodeSessionLink& link);
// Parse a persisted link (object under the conversation's "chudcode" key). Returns false
// (out untouched) when the value carries nothing usable.
bool chudcodeSessionLinkFromJson(const nlohmann::json& j, ChudcodeSessionLink& out);

// Registry keyed by Chudware conversation id. GL/UI-thread only (matching every other
// copilot-resident store); the tests drive it single-threaded.
const ChudcodeSessionLink* chudcodeSessionLink(const std::string& convId);
ChudcodeSessionLink&       chudcodeSessionLinkEnsure(const std::string& convId);
void chudcodeSessionLinkSet(const std::string& convId, ChudcodeSessionLink link);
void chudcodeSessionLinkErase(const std::string& convId);
// persistChatHistory re-keys a duplicate session id in place — the link follows the
// transcript it describes. No-op when oldId has no link; an existing newId link is replaced.
void chudcodeSessionLinkRekey(const std::string& oldId, const std::string& newId);
// loadChatHistory rebuilds the resident set from disk — the registry rebuilds with it.
void chudcodeSessionLinksClear();

// Conversation-JSON persistence seam (the exact calls conversations.cpp makes, so the tests
// exercise the shipped behavior):
//   persist — writes convObj["chudcode"] from the registry; touches NOTHING else in convObj
//             (the transcript is Chudware's own store — the link never duplicates it).
//   restore — reads convObj["chudcode"] into the registry for convId; absent/empty key
//             leaves the registry without an entry.
void chudcodeLinkPersist(const std::string& convId, nlohmann::json& convObj);
void chudcodeLinkRestore(const std::string& convId, const nlohmann::json& convObj);

// ---- request params ---------------------------------------------------------------------------
nlohmann::json chudcodeExportSessionParams(const std::string& sessionId,
                                           const std::string& outputPath = std::string());
nlohmann::json chudcodeBranchSessionParams(const std::string& sessionId,
                                           const std::string& entryId);
nlohmann::json chudcodeRepairSessionParams(const std::string& sessionId, bool repair);
nlohmann::json chudcodeCloseSessionParams(const std::string& sessionId);
// chudcode_rewind_session {session_id, user_text, occurrence, session_file?, workspace_dir?}.
// session_file/workspace_dir let the service recover an idle, not-live session from its bound
// file (fresh app restart); both are omitted when empty. Reply is the branch envelope —
// parse with parseChudcodeBranchResult.
nlohmann::json chudcodeRewindSessionParams(const std::string& sessionId,
                                           const std::string& userText, int occurrence,
                                           const std::string& sessionFile = std::string(),
                                           const std::string& workspaceDir = std::string());

// ---- edit-message rewind target ---------------------------------------------------------------
// The runtime matches the TYPED text of a user message: its persisted prompt is exactly the
// typed text, or the typed text + a blank line + one of the blocks the app/worker appends
// (attachments, context refs, plan edits, editor context). The GUI bubble is the typed text
// plus marker lines ("\n[attached image: …]", "\n[attached: …]"). These helpers are the GUI
// half of that rule.
// Typed text of a user bubble: trailing attachment marker lines stripped, then trimmed.
std::string chudcodeEditTypedText(const std::string& bubbleText);
// Typed texts match when equal after trimming. An empty target matches nothing.
bool chudcodeEditTextMatches(const std::string& candidate, const std::string& target);
// Zero-based occurrence of `target` among the typed texts of the user messages BEFORE the
// edited one (the count of earlier matches under chudcodeEditTextMatches).
int chudcodeEditOccurrence(const std::vector<std::string>& earlierTypedTexts,
                           const std::string& target);

// ---- response envelopes -----------------------------------------------------------------------
// Human-readable failure text from a worker refusal ({ok:false, reason|error}) or a
// non-object response; `fallback` when the envelope carries no text of its own.
std::string chudcodeActionError(const nlohmann::json& r, const char* fallback);

// True when the response says the method itself is absent (older worker / slice not
// deployed) — callers latch the whole surface off instead of retrying forever. Mirrors
// settings.cpp bridgeRpcAbsent, expressed over primitives so this TU stays AppState-free.
bool chudcodeSessionRpcAbsent(bool transportOk, const std::string& transportErr,
                              const nlohmann::json& result);

// chudcode_export_session -> {ok, path}. False when not an ok envelope or path missing.
struct ChudcodeExportResult {
    std::string path;
};
bool parseChudcodeExportResult(const nlohmann::json& r, ChudcodeExportResult& out);

// True for the worker's clean "session_not_live" refusal (export/branch/repair on an
// inactive runtime). Callers drop runtimeLive and show the resume hint instead of an error.
bool chudcodeSessionNotLive(const nlohmann::json& r);

// chudcode_branch_session -> {ok, chudcode_session_id, chudcode_session_file}.
// Desktop requires both resume fields; web requires only the opaque cell handle.
struct ChudcodeBranchResult {
    std::string sessionId;
    std::string sessionFile;
};
bool parseChudcodeBranchResult(const nlohmann::json& r, ChudcodeBranchResult& out);

// chudcode_repair_session -> bounded doctor summary (both repair:false dry-runs and
// repair:true runs use the same shape; backup_path appears only when a repair wrote one).
struct ChudcodeDoctorFinding {
    std::string code;     // malformed_jsonl | orphaned_tool_call | ... (verbatim)
    std::string message;
    std::string entryId;  // Chudcode DAG entry id, when the finding is entry-scoped
    std::string path;     // value path inside the entry, when reported
    bool repairable = false;
};
struct ChudcodeDoctorSummary {
    std::string mode;     // "dry-run" | "repair"
    std::string status;   // healthy | issues | repaired | repaired_with_issues
    bool changed = false;
    int findingCount = -1;            // -1 = unreported; derive from findings.size() then
    int repairableFindingCount = -1;
    int remainingFindingCount = -1;
    std::vector<ChudcodeDoctorFinding> findings;
    std::string sessionId;    // chudcode_session_id, when reported
    std::string sessionFile;  // chudcode_session_file, when reported
    std::string backupPath;   // repair-mode backup, when one was written
};
bool parseChudcodeDoctorSummary(const nlohmann::json& r, ChudcodeDoctorSummary& out);

// chudcode_close_session -> {ok} (idempotent: closing an already-closed runtime is ok:true).
bool parseChudcodeCloseResult(const nlohmann::json& r);

// ---- presentation-free helpers ----------------------------------------------------------------
// Doctor status vocabulary -> a UI severity bucket: 0 healthy/repaired (ok), 1 issues /
// repaired_with_issues (warn), 2 anything else incl. empty (unknown).
int chudcodeHealthClass(const std::string& health);
// Short human label for a health string; "not checked" for empty/unknown.
const char* chudcodeHealthLabel(const std::string& health);

// Stamp a doctor summary into a link: health always follows the summary's status; the
// id/file keys are adopted only when the summary reports them (never blanked).
void applyChudcodeDoctor(ChudcodeSessionLink& link, const ChudcodeDoctorSummary& s);

}  // namespace ui
