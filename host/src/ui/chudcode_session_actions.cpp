// Chudcode session recovery actions — see chudcode_session_actions.h. GL-free on purpose:
// host/tests/test_chudcode_session_actions.cpp links this exact TU.
#include "chudcode_session_actions.h"
#ifdef __EMSCRIPTEN__
#include "core/jobs.h"
#include "core/fs_backend.h"
#endif

#include <unordered_map>
#include <utility>

namespace ui {

namespace {

std::unordered_map<std::string, ChudcodeSessionLink>& linkRegistry() {
    static std::unordered_map<std::string, ChudcodeSessionLink> reg;
    return reg;
}

std::string jsonStr(const nlohmann::json& o, const char* key) {
    if (!o.is_object()) return {};
    const auto it = o.find(key);
    if (it == o.end() || !it->is_string()) return {};
    return it->get<std::string>();
}

// First present string among two spellings (worker snake_case, Chudcode-native camelCase).
std::string jsonStr2(const nlohmann::json& o, const char* a, const char* b) {
    std::string v = jsonStr(o, a);
    return v.empty() ? jsonStr(o, b) : v;
}

int jsonInt(const nlohmann::json& o, const char* a, const char* b, int absent) {
    if (!o.is_object()) return absent;
    for (const char* key : {a, b}) {
        const auto it = o.find(key);
        if (it != o.end() && it->is_number_integer()) return it->get<int>();
    }
    return absent;
}

bool okEnvelope(const nlohmann::json& r) {
    return r.is_object() && r.value("ok", false);
}

}  // namespace

// ---- link JSON round-trip ---------------------------------------------------------------------

nlohmann::json chudcodeSessionLinkToJson(const ChudcodeSessionLink& link) {
    if (link.empty()) return nlohmann::json();  // null — caller skips the key
    nlohmann::json j = nlohmann::json::object();
    if (!link.sessionId.empty()) j["session_id"] = link.sessionId;
    if (!link.sessionFile.empty()) j["session_file"] = link.sessionFile;
    if (!link.health.empty()) j["health"] = link.health;
    return j;
}

bool chudcodeSessionLinkFromJson(const nlohmann::json& j, ChudcodeSessionLink& out) {
    if (!j.is_object()) return false;
    ChudcodeSessionLink parsed;
    parsed.sessionId = jsonStr2(j, "session_id", "sessionId");
    parsed.sessionFile = jsonStr2(j, "session_file", "sessionFile");
    parsed.health = jsonStr(j, "health");
    if (parsed.empty()) return false;
    out = std::move(parsed);
    return true;
}

// ---- registry ---------------------------------------------------------------------------------

const ChudcodeSessionLink* chudcodeSessionLink(const std::string& convId) {
    auto& reg = linkRegistry();
    const auto it = reg.find(convId);
    return it == reg.end() ? nullptr : &it->second;
}

ChudcodeSessionLink& chudcodeSessionLinkEnsure(const std::string& convId) {
    return linkRegistry()[convId];
}

void chudcodeSessionLinkSet(const std::string& convId, ChudcodeSessionLink link) {
#ifdef __EMSCRIPTEN__
    link.sessionFile.clear();
    if (!link.sessionId.empty()) bindCellAgentSessionHandle(convId, link.sessionId);
#endif
    linkRegistry()[convId] = std::move(link);
}

void chudcodeSessionLinkErase(const std::string& convId) {
#ifdef __EMSCRIPTEN__
    bindCellAgentSessionHandle(convId, {});
#endif
    linkRegistry().erase(convId);
}

void chudcodeSessionLinkRekey(const std::string& oldId, const std::string& newId) {
    if (oldId == newId) return;
#ifdef __EMSCRIPTEN__
    const std::string handle = cellAgentSessionHandle(oldId);
    bindCellAgentSessionHandle(newId, handle, cellAgentSessionProvider(oldId));
    bindCellAgentSessionHandle(oldId, {});
#endif
    auto& reg = linkRegistry();
    const auto it = reg.find(oldId);
    if (it == reg.end()) return;
    reg[newId] = std::move(it->second);
    reg.erase(oldId);
}

void chudcodeSessionLinksClear() {
#ifdef __EMSCRIPTEN__
    clearCellAgentSessionHandles();
#endif
    linkRegistry().clear();
}

void chudcodeLinkPersist(const std::string& convId, nlohmann::json& convObj) {
    const ChudcodeSessionLink* link = chudcodeSessionLink(convId);
    if (!link) return;
    nlohmann::json j = chudcodeSessionLinkToJson(*link);
    if (j.is_object() && !j.empty()) convObj["chudcode"] = std::move(j);
}

void chudcodeLinkRestore(const std::string& convId, const nlohmann::json& convObj) {
    if (!convObj.is_object()) return;
#ifdef __EMSCRIPTEN__
    // Only explicit cell provenance may resume a browser session. Old native
    // runtime ids and JSONL paths are not handles and must never be reinterpreted.
    const auto handle = convObj.find("cell_session_id");
    if (handle == convObj.end() || !handle->is_string() || handle->get_ref<const std::string&>().empty())
        return;
    ChudcodeSessionLink link;
    const auto it = convObj.find("chudcode");
    if (it != convObj.end()) chudcodeSessionLinkFromJson(*it, link);
    link.sessionId = handle->get<std::string>();
    link.sessionFile.clear();
    const auto provider = convObj.find("cell_session_provider");
    if (provider != convObj.end() && provider->is_string())
        bindCellAgentSessionHandle(convId, link.sessionId, provider->get<std::string>());
    chudcodeSessionLinkSet(convId, std::move(link));
#else
    const auto it = convObj.find("chudcode");
    if (it == convObj.end()) return;
    ChudcodeSessionLink link;
    if (chudcodeSessionLinkFromJson(*it, link)) chudcodeSessionLinkSet(convId, std::move(link));
#endif
}

// ---- request params ---------------------------------------------------------------------------

nlohmann::json chudcodeExportSessionParams(const std::string& sessionId,
                                           const std::string& outputPath) {
#ifdef __EMSCRIPTEN__
    nlohmann::json p = {{"session_id", cellAgentSessionHandle(sessionId)}};
#else
    nlohmann::json p = {{"session_id", sessionId}};
#endif
    if (!outputPath.empty()) p["output_path"] = outputPath;
    return p;
}

nlohmann::json chudcodeBranchSessionParams(const std::string& sessionId,
                                           const std::string& entryId) {
#ifdef __EMSCRIPTEN__
    return {{"session_id", cellAgentSessionHandle(sessionId)}, {"entry_id", entryId}};
#else
    return {{"session_id", sessionId}, {"entry_id", entryId}};
#endif
}

nlohmann::json chudcodeRepairSessionParams(const std::string& sessionId, bool repair) {
#ifdef __EMSCRIPTEN__
    return {{"session_id", cellAgentSessionHandle(sessionId)}, {"repair", repair}};
#else
    return {{"session_id", sessionId}, {"repair", repair}};
#endif
}

nlohmann::json chudcodeCloseSessionParams(const std::string& sessionId) {
#ifdef __EMSCRIPTEN__
    return {{"session_id", cellAgentSessionHandle(sessionId)},
            {"provider", cellAgentSessionProvider(sessionId)}};
#else
    return {{"session_id", sessionId}};
#endif
}

nlohmann::json chudcodeRewindSessionParams(const std::string& sessionId,
                                           const std::string& userText, int occurrence,
                                           const std::string& sessionFile,
                                           const std::string& workspaceDir) {
#ifdef __EMSCRIPTEN__
    (void)sessionFile;
    nlohmann::json p = {{"session_id", cellAgentSessionHandle(sessionId)},
                        {"user_text", userText},
                        {"occurrence", occurrence < 0 ? 0 : occurrence}};
    std::string relative, err;
    if (chudware::workspaceRelativePath(workspaceDir, relative, err))
        p["workspace_dir"] = relative.empty() ? "." : relative;
#else
    nlohmann::json p = {{"session_id", sessionId},
                        {"user_text", userText},
                        {"occurrence", occurrence < 0 ? 0 : occurrence}};
    if (!sessionFile.empty()) p["session_file"] = sessionFile;
    if (!workspaceDir.empty()) p["workspace_dir"] = workspaceDir;
#endif
    return p;
}

// ---- edit-message rewind target ---------------------------------------------------------------

static std::string trimWs(const std::string& s) {
    const char* ws = " \t\r\n";
    const size_t b = s.find_first_not_of(ws);
    if (b == std::string::npos) return {};
    return s.substr(b, s.find_last_not_of(ws) - b + 1);
}

std::string chudcodeEditTypedText(const std::string& bubbleText) {
    std::string t = bubbleText;
    // userBubbleText appends one "\n[attached image: name]" line per image, then at most one
    // "\n[attached: a b]" roster — strip those marker lines from the end only.
    for (;;) {
        const size_t nl = t.rfind('\n');
        if (nl == std::string::npos) break;
        const std::string line = t.substr(nl + 1);
        const bool marker = !line.empty() && line.back() == ']' &&
                            (line.rfind("[attached image: ", 0) == 0 ||
                             line.rfind("[attached:", 0) == 0);
        if (!marker) break;
        t.erase(nl);
    }
    return trimWs(t);
}

bool chudcodeEditTextMatches(const std::string& candidate, const std::string& target) {
    // GUI typed texts carry none of the runtime-appended blocks, so the runtime's rule
    // ("exact, or exact + a known appended block") reduces to trimmed equality here.
    const std::string tgt = trimWs(target);
    return !tgt.empty() && trimWs(candidate) == tgt;
}

int chudcodeEditOccurrence(const std::vector<std::string>& earlierTypedTexts,
                           const std::string& target) {
    int n = 0;
    for (const auto& t : earlierTypedTexts)
        if (chudcodeEditTextMatches(t, target)) ++n;
    return n;
}

// ---- response envelopes -----------------------------------------------------------------------

std::string chudcodeActionError(const nlohmann::json& r, const char* fallback) {
    if (r.is_object()) {
        std::string reason = jsonStr(r, "reason");
        if (reason.empty()) reason = jsonStr(r, "error");
        if (!reason.empty()) return reason;
    }
    return fallback ? std::string(fallback) : std::string();
}

bool chudcodeSessionNotLive(const nlohmann::json& r) {
    if (!r.is_object() || r.value("ok", true)) return false;
    const std::string reason = chudcodeActionError(r, "");
    return reason.find("session_not_live") != std::string::npos ||
           reason.find("not live") != std::string::npos;
}

bool chudcodeSessionRpcAbsent(bool transportOk, const std::string& transportErr,
                              const nlohmann::json& result) {
    if (!transportOk) {
        return transportErr.find("unknown") != std::string::npos ||
               transportErr.find("not found") != std::string::npos ||
               transportErr.find("not_found") != std::string::npos ||
               transportErr.find("method") != std::string::npos;
    }
    if (!result.is_object()) return false;
    auto absentCode = [](const std::string& c) {
        return c == "unknown_method" || c == "method_not_found" || c == "not_implemented";
    };
    if (absentCode(jsonStr(result, "code"))) return true;
    return !result.value("ok", true) && absentCode(jsonStr(result, "reason"));
}

bool parseChudcodeExportResult(const nlohmann::json& r, ChudcodeExportResult& out) {
    if (!okEnvelope(r)) return false;
    std::string path = jsonStr2(r, "path", "output_path");
    if (path.empty()) path = jsonStr(r, "outputPath");
    if (path.empty()) return false;
    out.path = std::move(path);
    return true;
}

bool parseChudcodeBranchResult(const nlohmann::json& r, ChudcodeBranchResult& out) {
    if (!okEnvelope(r)) return false;
    ChudcodeBranchResult parsed;
    parsed.sessionId = jsonStr2(r, "chudcode_session_id", "sessionId");
    parsed.sessionFile = jsonStr2(r, "chudcode_session_file", "sessionFile");
#ifdef __EMSCRIPTEN__
    // A browser branch resumes by its cell-owned handle, never a runtime file.
    parsed.sessionFile.clear();
    if (parsed.sessionId.empty()) return false;
#else
    if (parsed.sessionId.empty() || parsed.sessionFile.empty()) return false;
#endif
    out = std::move(parsed);
    return true;
}

bool parseChudcodeDoctorSummary(const nlohmann::json& r, ChudcodeDoctorSummary& out) {
    if (!okEnvelope(r)) return false;
    ChudcodeDoctorSummary s;
    s.mode = jsonStr(r, "mode");
    s.status = jsonStr(r, "status");
    if (s.status.empty()) return false;  // a summary without a verdict is not a summary
    s.changed = r.value("changed", false);
    s.findingCount = jsonInt(r, "finding_count", "findingCount", -1);
    s.repairableFindingCount =
        jsonInt(r, "repairable_finding_count", "repairableFindingCount", -1);
    s.remainingFindingCount =
        jsonInt(r, "remaining_finding_count", "remainingFindingCount", -1);
    const auto findingsIt = r.find("findings");
    if (findingsIt != r.end() && findingsIt->is_array()) {
        for (const auto& f : *findingsIt) {
            if (!f.is_object()) continue;
            ChudcodeDoctorFinding row;
            row.code = jsonStr(f, "code");
            row.message = jsonStr(f, "message");
            if (row.code.empty() && row.message.empty()) continue;  // nothing to show
            row.repairable = f.value("repairable", false);
            row.entryId = jsonStr2(f, "entry_id", "entryId");
            row.path = jsonStr(f, "path");
            s.findings.push_back(std::move(row));
        }
    }
    if (s.findingCount < 0) s.findingCount = (int)s.findings.size();
    s.sessionId = jsonStr2(r, "chudcode_session_id", "sessionId");
    s.sessionFile = jsonStr2(r, "chudcode_session_file", "sessionFile");
    s.backupPath = jsonStr2(r, "backup_path", "backupPath");
    out = std::move(s);
    return true;
}

bool parseChudcodeCloseResult(const nlohmann::json& r) { return okEnvelope(r); }

// ---- presentation-free helpers ----------------------------------------------------------------

int chudcodeHealthClass(const std::string& health) {
    if (health == "healthy" || health == "repaired") return 0;
    if (health == "issues" || health == "repaired_with_issues") return 1;
    return 2;
}

const char* chudcodeHealthLabel(const std::string& health) {
    if (health == "healthy") return "healthy";
    if (health == "issues") return "issues found";
    if (health == "repaired") return "repaired";
    if (health == "repaired_with_issues") return "repaired, issues remain";
    return "not checked";
}

void applyChudcodeDoctor(ChudcodeSessionLink& link, const ChudcodeDoctorSummary& s) {
    if (!s.status.empty()) link.health = s.status;
    if (!s.sessionId.empty()) link.sessionId = s.sessionId;
#ifdef __EMSCRIPTEN__
    link.sessionFile.clear();
#else
    if (!s.sessionFile.empty()) link.sessionFile = s.sessionFile;
#endif
}

}  // namespace ui
