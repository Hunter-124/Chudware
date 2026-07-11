#include "autosave.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <vector>

#ifdef _WIN32
#include <process.h>  // _getpid
#define AICAD_GETPID _getpid
#else
#include <unistd.h>
#define AICAD_GETPID getpid
#endif

#include "../../services/worker/cpp_core.h"
#include "hostd_log.h"

namespace hostd {

namespace fs = std::filesystem;
using nlohmann::json;

static long long fileMtimeMs(const fs::path& p) {
    std::error_code ec;
    const auto t = fs::last_write_time(p, ec);
    if (ec) return 0;
    return std::chrono::duration_cast<std::chrono::milliseconds>(t.time_since_epoch())
        .count();
}

void Autosave::start(const std::string& baseDir, const std::atomic<int>* engineState) {
    baseDir_ = baseDir;
    orphanDir_ = baseDir_ + "/orphaned";
    engineState_ = engineState;
    if (const char* v = std::getenv("AI_CAD_AUTOSAVE_INTERVAL_S"); v && *v)
        intervalS_ = std::atof(v);
    const long long startMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                                  std::chrono::system_clock::now().time_since_epoch())
                                  .count();
    char sid[64];
    std::snprintf(sid, sizeof sid, "%015lld-%ld", startMs, (long)AICAD_GETPID());
    sessionDir_ = baseDir_ + "/" + sid;
    thread_ = std::thread([this] { loop(); });
}

void Autosave::stop(bool finalSave) {
    stopping_ = true;
    cv_.notify_all();
    if (thread_.joinable()) thread_.join();
    // Final save runs even when the periodic loop is disabled (interval 0):
    // "disable" means no background ticks, not "lose dirty docs on drain".
    if (finalSave && engineState_ && engineState_->load() == 1) {
        saveDirtyDocs();
        retireOpenDocsOnExit();
    }
    // Clean shutdown: remove this session's dir so the next start does not
    // crash-recover docs that were deliberately retired (or had nothing to
    // save). A hard kill skips this — that IS the crash-recovery signal. The
    // crash-tier source we recovered from is equally settled by now (every
    // doc from it was retired, closed, or re-saved), so it goes too.
    std::error_code ec;
    if (!sessionDir_.empty()) fs::remove_all(sessionDir_, ec);
    if (!recoverDir_.empty()) fs::remove_all(recoverDir_, ec);
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
    // The orphan tier lives beside the session dirs and is never auto-opened.
    std::vector<std::string> sessions;
    for (const auto& e : fs::directory_iterator(baseDir_, ec))
        if (e.is_directory() && e.path().filename() != "orphaned")
            sessions.push_back(e.path().filename().string());
    if (sessions.empty()) return;
    std::sort(sessions.begin(), sessions.end());
    const std::string latest = sessions.back();

    // Sweep everything OLDER than the most recent prior session.
    for (const auto& s : sessions)
        if (s != latest) fs::remove_all(baseDir_ + "/" + s, ec);

    recoverDir_ = baseDir_ + "/" + latest;
    int recovered = 0;
    for (const auto& e : fs::directory_iterator(recoverDir_, ec)) {
        if (e.path().extension() != ".step+") continue;
        const std::string oldId = e.path().stem().string();
        json result;
        std::string err;
        // detach_path: a crash-recovered doc must not keep pointing INTO the autosave
        // tier — a user save would land there, and the artifact is transient anyway.
        if (!core::cpp_core().call(
                "doc_open",
                json{{"path", e.path().string()}, {"detach_path", true}}, result, err)) {
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
        // The doc came back detached (clean of any autosave path) and not dirty, so
        // the tick won't re-save it until the first edit. Carry the artifact into
        // THIS session's dir under the new id so a crash in that window still recovers.
        fs::create_directories(sessionDir_, ec);
        fs::copy_file(e.path(), sessionDir_ + "/" + newId + ".step+",
                      fs::copy_options::overwrite_existing, ec);
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

// ------------------------------------------------------------- orphan tier

void Autosave::onDocClosed(const std::string& docId) {
    if (docId.empty()) return;
    std::error_code ec;
    fs::remove(sessionDir_ + "/" + docId + ".step+", ec);
    lastSavedRev_.erase(docId);
    // A crash-recovered doc closed on purpose must not come back on the next
    // crash: drop its source file and remap entry too.
    std::lock_guard<std::mutex> lk(remapMtx_);
    for (auto it = idRemap_.begin(); it != idRemap_.end(); ++it) {
        if (it.value() == docId) {
            if (!recoverDir_.empty())
                fs::remove(recoverDir_ + "/" + it.key() + ".step+", ec);
            idRemap_.erase(it);
            break;
        }
    }
}

bool Autosave::retireDoc(const std::string& docId, const std::string& name) {
    if (docId.empty()) return false;
    std::error_code ec;
    fs::create_directories(orphanDir_, ec);
    const long long nowMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::system_clock::now().time_since_epoch())
                                .count();
    char stem[128];
    std::snprintf(stem, sizeof stem, "%015lld-%s", nowMs, docId.c_str());
    const std::string base = orphanDir_ + "/" + stem;
    json result;
    std::string err;
    if (!core::cpp_core().call("doc_save", json{{"doc_id", docId}, {"path", base + ".step+"}},
                               result, err)) {
        log().line("doc-gc: retire doc_save(%s) failed: %s", docId.c_str(), err.c_str());
        return false;
    }
    {
        std::lock_guard<std::mutex> lk(orphanMtx_);
        std::ofstream meta(base + ".json", std::ios::trunc);
        meta << json{{"doc_id", docId}, {"name", name}, {"retired_ms", nowMs}}.dump();
    }
    fs::remove(sessionDir_ + "/" + docId + ".step+", ec);
    lastSavedRev_.erase(docId);
    sweepOrphans();
    return true;
}

void Autosave::retireOpenDocsOnExit() {
    json docs;
    std::string err;
    if (!core::cpp_core().call("doc_list", json::object(), docs, err)) return;
    if (!docs.is_object() || !docs.contains("docs")) return;
    for (const auto& d : docs["docs"]) {
        if (!d.is_object()) continue;
        const std::string id = d.value("doc_id", std::string());
        if (id.empty()) continue;
        // A clean doc that lives in a USER file needs no orphan copy — the user
        // reopens the file. Autosave artifacts don't count (doc_save set them as
        // the doc's path); those docs are retired like unsaved ones.
        const std::string path = d.contains("path") && d["path"].is_string()
                                     ? d["path"].get<std::string>()
                                     : std::string();
        if (!path.empty() && !isAutosavePath(path) && !d.value("dirty", false)) continue;
        retireDoc(id, d.value("name", id));
    }
}

json Autosave::recoveryList() {
    json entries = json::array();
    std::error_code ec;
    std::vector<fs::path> files;
    for (const auto& e : fs::directory_iterator(orphanDir_, ec))
        if (e.path().extension() == ".step+") files.push_back(e.path());
    std::sort(files.begin(), files.end(), std::greater<>());  // newest stamp first
    for (const auto& p : files) {
        const std::string stem = p.stem().string();
        json meta = json::object();
        {
            std::lock_guard<std::mutex> lk(orphanMtx_);
            std::ifstream in(orphanDir_ + "/" + stem + ".json");
            if (in) {
                try {
                    in >> meta;
                } catch (...) {
                    meta = json::object();
                }
            }
        }
        entries.push_back({{"id", stem},
                           {"doc_id", meta.value("doc_id", stem)},
                           {"name", meta.value("name", stem)},
                           {"path", p.string()},
                           {"mtime_ms", fileMtimeMs(p)}});
    }
    return json{{"entries", entries}};
}

int Autosave::orphanCount() {
    int n = 0;
    std::error_code ec;
    for (const auto& e : fs::directory_iterator(orphanDir_, ec))
        if (e.path().extension() == ".step+") ++n;
    return n;
}

std::string Autosave::orphanPath(const std::string& id) {
    if (id.empty() || id.find('/') != std::string::npos ||
        id.find("..") != std::string::npos)
        return {};
    const std::string p = orphanDir_ + "/" + id + ".step+";
    std::error_code ec;
    return fs::exists(p, ec) ? p : std::string();
}

int Autosave::discardOrphans(const std::string& id) {
    std::lock_guard<std::mutex> lk(orphanMtx_);
    std::error_code ec;
    int n = 0;
    if (!id.empty()) {
        if (id.find('/') != std::string::npos || id.find("..") != std::string::npos)
            return 0;
        if (fs::remove(orphanDir_ + "/" + id + ".step+", ec)) ++n;
        fs::remove(orphanDir_ + "/" + id + ".json", ec);
        return n;
    }
    for (const auto& e : fs::directory_iterator(orphanDir_, ec)) {
        if (e.path().extension() == ".step+") ++n;
        fs::remove(e.path(), ec);
    }
    return n;
}

void Autosave::sweepOrphans() {
    int keep = 20;
    if (const char* v = std::getenv("AICAD_ORPHAN_KEEP"); v && *v)
        keep = std::max(0, std::atoi(v));
    std::lock_guard<std::mutex> lk(orphanMtx_);
    std::error_code ec;
    std::vector<std::string> stems;
    for (const auto& e : fs::directory_iterator(orphanDir_, ec))
        if (e.path().extension() == ".step+") stems.push_back(e.path().stem().string());
    if ((int)stems.size() <= keep) return;
    std::sort(stems.begin(), stems.end());  // oldest stamp first
    const int drop = (int)stems.size() - keep;
    for (int i = 0; i < drop; ++i) {
        fs::remove(orphanDir_ + "/" + stems[i] + ".step+", ec);
        fs::remove(orphanDir_ + "/" + stems[i] + ".json", ec);
    }
}

}  // namespace hostd
