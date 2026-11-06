// Omp session recovery actions — see omp_session_actions.h. GL-free on purpose:
// host/tests/test_omp_session_actions.cpp links this exact TU.
#include "omp_session_actions.h"
#ifdef __EMSCRIPTEN__
#include "core/jobs.h"
#endif

#include <unordered_map>
#include <utility>

namespace ui {

namespace {

std::unordered_map<std::string, OmpSessionLink>& linkRegistry() {
    static std::unordered_map<std::string, OmpSessionLink> reg;
    return reg;
}

std::string jsonStr(const nlohmann::json& o, const char* key) {
    if (!o.is_object()) return {};
    const auto it = o.find(key);
    if (it == o.end() || !it->is_string()) return {};
    return it->get<std::string>();
}

// First present string among two spellings (worker snake_case, Omp-native camelCase).
std::string jsonStr2(const nlohmann::json& o, const char* a, const char* b) {
    std::string v = jsonStr(o, a);
    return v.empty() ? jsonStr(o, b) : v;
}


bool okEnvelope(const nlohmann::json& r) {
    return r.is_object() && r.value("ok", false);
}

}  // namespace

// ---- link JSON round-trip ---------------------------------------------------------------------

nlohmann::json ompSessionLinkToJson(const OmpSessionLink& link) {
    if (link.empty()) return nlohmann::json();  // null — caller skips the key
    nlohmann::json j = nlohmann::json::object();
    if (!link.sessionId.empty()) j["session_id"] = link.sessionId;
    if (!link.sessionFile.empty()) j["session_file"] = link.sessionFile;
    if (!link.health.empty()) j["health"] = link.health;
    return j;
}

bool ompSessionLinkFromJson(const nlohmann::json& j, OmpSessionLink& out) {
    if (!j.is_object()) return false;
    OmpSessionLink parsed;
    parsed.sessionId = jsonStr2(j, "session_id", "sessionId");
    parsed.sessionFile = jsonStr2(j, "session_file", "sessionFile");
    parsed.health = jsonStr(j, "health");
    if (parsed.empty()) return false;
    out = std::move(parsed);
    return true;
}

// ---- registry ---------------------------------------------------------------------------------

const OmpSessionLink* ompSessionLink(const std::string& convId) {
    auto& reg = linkRegistry();
    const auto it = reg.find(convId);
    return it == reg.end() ? nullptr : &it->second;
}

OmpSessionLink& ompSessionLinkEnsure(const std::string& convId) {
    return linkRegistry()[convId];
}

void ompSessionLinkSet(const std::string& convId, OmpSessionLink link) {
#ifdef __EMSCRIPTEN__
    link.sessionFile.clear();
    if (!link.sessionId.empty()) bindCellAgentSessionHandle(convId, link.sessionId);
#endif
    linkRegistry()[convId] = std::move(link);
}

void ompSessionLinkErase(const std::string& convId) {
#ifdef __EMSCRIPTEN__
    bindCellAgentSessionHandle(convId, {});
#endif
    linkRegistry().erase(convId);
}

void ompSessionLinkRekey(const std::string& oldId, const std::string& newId) {
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

void ompSessionLinksClear() {
#ifdef __EMSCRIPTEN__
    clearCellAgentSessionHandles();
#endif
    linkRegistry().clear();
}

void ompLinkPersist(const std::string& convId, nlohmann::json& convObj) {
    // Saved snapshots may still carry the old key; writing is the one-time migration.
    convObj.erase("chudcode");
    const OmpSessionLink* link = ompSessionLink(convId);
    if (!link) return;
    nlohmann::json j = ompSessionLinkToJson(*link);
    if (j.is_object() && !j.empty()) convObj["omp"] = std::move(j);
}

void ompLinkRestore(const std::string& convId, const nlohmann::json& convObj) {
    if (!convObj.is_object()) return;
#ifdef __EMSCRIPTEN__
    // Only explicit cell provenance may resume a browser session. Old native
    // runtime ids and JSONL paths are not handles and must never be reinterpreted.
    const auto handle = convObj.find("cell_session_id");
    if (handle == convObj.end() || !handle->is_string() || handle->get_ref<const std::string&>().empty())
        return;
    OmpSessionLink link;
    auto it = convObj.find("omp");
    if (it == convObj.end()) it = convObj.find("chudcode");
    if (it != convObj.end()) ompSessionLinkFromJson(*it, link);
    link.sessionId = handle->get<std::string>();
    link.sessionFile.clear();
    const auto provider = convObj.find("cell_session_provider");
    if (provider != convObj.end() && provider->is_string())
        bindCellAgentSessionHandle(convId, link.sessionId, provider->get<std::string>());
    ompSessionLinkSet(convId, std::move(link));
#else
    auto it = convObj.find("omp");
    if (it == convObj.end()) it = convObj.find("chudcode");
    if (it == convObj.end()) return;
    OmpSessionLink link;
    if (ompSessionLinkFromJson(*it, link)) ompSessionLinkSet(convId, std::move(link));
#endif
}

// ---- request params ---------------------------------------------------------------------------

nlohmann::json ompExportSessionParams(const std::string& sessionId,
                                           const std::string& outputPath) {
#ifdef __EMSCRIPTEN__
    nlohmann::json p = {{"session_id", cellAgentSessionHandle(sessionId)}};
#else
    nlohmann::json p = {{"session_id", sessionId}};
#endif
    if (!outputPath.empty()) p["output_path"] = outputPath;
    return p;
}

nlohmann::json ompBranchSessionParams(const std::string& sessionId,
                                           const std::string& entryId) {
#ifdef __EMSCRIPTEN__
    return {{"session_id", cellAgentSessionHandle(sessionId)}, {"entry_id", entryId}};
#else
    return {{"session_id", sessionId}, {"entry_id", entryId}};
#endif
}

nlohmann::json ompCloseSessionParams(const std::string& sessionId) {
#ifdef __EMSCRIPTEN__
    return {{"session_id", cellAgentSessionHandle(sessionId)},
            {"provider", cellAgentSessionProvider(sessionId)}};
#else
    return {{"session_id", sessionId}};
#endif
}

// ---- response envelopes -----------------------------------------------------------------------

std::string ompActionError(const nlohmann::json& r, const char* fallback) {
    if (r.is_object()) {
        std::string reason = jsonStr(r, "reason");
        if (reason.empty()) reason = jsonStr(r, "error");
        if (!reason.empty()) return reason;
    }
    return fallback ? std::string(fallback) : std::string();
}

bool ompSessionNotLive(const nlohmann::json& r) {
    if (!r.is_object() || r.value("ok", true)) return false;
    const std::string reason = ompActionError(r, "");
    return reason.find("session_not_live") != std::string::npos ||
           reason.find("not live") != std::string::npos;
}

bool ompSessionRpcAbsent(bool transportOk, const std::string& transportErr,
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

bool parseOmpExportResult(const nlohmann::json& r, OmpExportResult& out) {
    if (!okEnvelope(r)) return false;
    std::string path = jsonStr2(r, "path", "output_path");
    if (path.empty()) path = jsonStr(r, "outputPath");
    if (path.empty()) return false;
    out.path = std::move(path);
    return true;
}

bool parseOmpBranchResult(const nlohmann::json& r, OmpBranchResult& out) {
    if (!okEnvelope(r)) return false;
    OmpBranchResult parsed;
    parsed.sessionId = jsonStr2(r, "omp_session_id", "sessionId");
    parsed.sessionFile = jsonStr2(r, "omp_session_file", "sessionFile");
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

bool parseOmpCloseResult(const nlohmann::json& r) { return okEnvelope(r); }

}  // namespace ui
