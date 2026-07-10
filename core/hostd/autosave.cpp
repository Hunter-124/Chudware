#include "autosave.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <vector>

#include <unistd.h>

#include "../../services/worker/cpp_core.h"
#include "hostd_log.h"

namespace hostd {

namespace fs = std::filesystem;
using nlohmann::json;

void Autosave::start(const std::string& baseDir, const std::atomic<int>* engineState) {
    baseDir_ = baseDir;
    engineState_ = engineState;
    if (const char* v = std::getenv("AI_CAD_AUTOSAVE_INTERVAL_S"); v && *v)
        intervalS_ = std::atof(v);
    const long long startMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                                  std::chrono::system_clock::now().time_since_epoch())
                                  .count();
    char sid[64];
    std::snprintf(sid, sizeof sid, "%015lld-%ld", startMs, (long)::getpid());
    sessionDir_ = baseDir_ + "/" + sid;
    thread_ = std::thread([this] { loop(); });
}

void Autosave::stop(bool finalSave) {
    stopping_ = true;
    cv_.notify_all();
    if (thread_.joinable()) thread_.join();
    // Final save runs even when the periodic loop is disabled (interval 0):
    // "disable" means no background ticks, not "lose dirty docs on drain".
    if (finalSave && engineState_ && engineState_->load() == 1) saveDirtyDocs();
}

json Autosave::idRemap() {
    std::lock_guard<std::mutex> lk(remapMtx_);
    return idRemap_;
}

void Autosave::loop() {
    // Wait for the engine (recovery needs doc_open). Bails on stop or load failure.
    while (!stopping_) {
        const int st = engineState_ ? engineState_->load() : 0;
        if (st == 1) break;
        if (st == 2) {
            recoveryDone_ = true;  // nothing to recover into; unblock waiters
            return;
        }
        std::unique_lock<std::mutex> lk(mtx_);
        cv_.wait_for(lk, std::chrono::milliseconds(200));
    }
    if (stopping_) return;

    recoverPriorSession();
    recoveryDone_ = true;

    if (intervalS_ <= 0) return;  // autosave disabled; recovery alone was wanted
    while (!stopping_) {
        {
            std::unique_lock<std::mutex> lk(mtx_);
            cv_.wait_for(lk, std::chrono::duration<double>(intervalS_),
                         [this] { return stopping_.load(); });
        }
        if (stopping_) break;
        saveDirtyDocs();
    }
}

void Autosave::recoverPriorSession() {
    std::error_code ec;
    fs::create_directories(baseDir_, ec);

    // Session dir names are zero-padded start-ms, so lexical max = most recent.
    std::vector<std::string> sessions;
    for (const auto& e : fs::directory_iterator(baseDir_, ec))
        if (e.is_directory()) sessions.push_back(e.path().filename().string());
    if (sessions.empty()) return;
    std::sort(sessions.begin(), sessions.end());
    const std::string latest = sessions.back();

    // Sweep everything OLDER than the most recent prior session.
    for (const auto& s : sessions)
        if (s != latest) fs::remove_all(baseDir_ + "/" + s, ec);

    int recovered = 0;
    for (const auto& e : fs::directory_iterator(baseDir_ + "/" + latest, ec)) {
        if (e.path().extension() != ".step+") continue;
        const std::string oldId = e.path().stem().string();
        json result;
        std::string err;
        if (!core::cpp_core().call("doc_open", json{{"path", e.path().string()}}, result,
                                   err)) {
            log().line("autosave recovery: doc_open(%s) failed: %s",
                       e.path().string().c_str(), err.c_str());
            continue;
        }
        const std::string newId = result.value("doc_id", std::string());
        if (newId.empty()) continue;
        {
            std::lock_guard<std::mutex> lk(remapMtx_);
            idRemap_[oldId] = newId;
        }
        ++recovered;
        log().line("autosave recovery: %s -> %s", oldId.c_str(), newId.c_str());
    }
    if (recovered) log().line("recovered %d doc(s) from session %s", recovered, latest.c_str());
}

void Autosave::saveDirtyDocs() {
    json docs;
    std::string err;
    if (!core::cpp_core().call("doc_list", json::object(), docs, err)) return;
    if (!docs.is_object() || !docs.contains("docs")) return;

    std::error_code ec;
    for (const auto& d : docs["docs"]) {
        if (!d.is_object() || !d.value("dirty", false)) continue;
        const std::string id = d.value("doc_id", std::string());
        if (id.empty()) continue;
        const long rev = d.contains("revision") && d["revision"].is_number()
                             ? d["revision"].get<long>()
                             : -1;
        auto it = lastSavedRev_.find(id);
        if (it != lastSavedRev_.end() && rev >= 0 && it->second == rev)
            continue;  // unchanged since last autosave — skip the full serialize
        fs::create_directories(sessionDir_, ec);
        json result;
        const std::string path = sessionDir_ + "/" + id + ".step+";
        if (core::cpp_core().call("doc_save", json{{"doc_id", id}, {"path", path}}, result,
                                  err)) {
            if (rev >= 0) lastSavedRev_[id] = rev;
        } else {
            log().line("autosave: doc_save(%s) failed: %s", id.c_str(), err.c_str());
        }
    }
}

}  // namespace hostd
