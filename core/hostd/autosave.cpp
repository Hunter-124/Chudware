#include "autosave.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <vector>

#ifdef _WIN32
#include <process.h>  // _getpid
#define CHUDWARE_GETPID _getpid
#else
#include <unistd.h>
#define CHUDWARE_GETPID getpid
#endif

#include "../../services/worker/cpp_core.h"
#include "hostd_log.h"
#include "../../services/ipc/ipc_private.h"

#include <miniz.h>

namespace hostd {

namespace fs = std::filesystem;
using nlohmann::json;

// True when a document JSON has no authored content worth recovering.
// Material/name alone do not count. Assemblies need zero components AND mates
// AND features. Parts need zero features (sketches live under features).
static bool documentJsonIsEmpty(const json& doc) {
    if (!doc.is_object()) return true;
    auto arrSize = [&](const char* key) -> int {
        if (!doc.contains(key) || !doc[key].is_array()) return 0;
        return (int)doc[key].size();
    };
    const int features = arrSize("features");
    const std::string schema = doc.value("schema", std::string());
    if (schema == "aicad.asm/1") {
        return features == 0 && arrSize("components") == 0 && arrSize("mates") == 0;
    }
    return features == 0;
}

// A doc_list row whose whole content is an unmodified import of source files that all
// still exist on disk (engine `reproducible_from`, core/doc/session.cpp doc_list) is
// reproducible scratch, not user work: re-importing the source rebuilds it. An agent's
// body-roster probe (`import_part(..., keep_bodies=[...])`) used to reach the recovery
// prompt this way (2026-09-24). Any authored change empties the list engine-side, and a
// missing source keeps the doc recovery-worthy.
static bool docListRowIsReproducible(const json& d) {
    if (!d.contains("reproducible_from") || !d["reproducible_from"].is_array() ||
        d["reproducible_from"].empty())
        return false;
    for (const auto& src : d["reproducible_from"]) {
        std::error_code ec;
        if (!src.is_string() || !fs::is_regular_file(src.get<std::string>(), ec)) return false;
    }
    return true;
}

// Peek document.json inside a .step+ ZIP without opening it in the engine.
// Returns true when the file is missing/unreadable/corrupt OR the doc is empty.
// Prefer false only when we can prove the part has content (fail-closed for recovery).
static bool stepPlusFileIsEmpty(const std::string& path) {
    mz_zip_archive zip;
    memset(&zip, 0, sizeof(zip));
    if (!mz_zip_reader_init_file(&zip, path.c_str(), 0)) return true;
    size_t size = 0;
    void* data = mz_zip_reader_extract_file_to_heap(&zip, "document.json", &size, 0);
    mz_zip_reader_end(&zip);
    if (!data || size == 0) {
        if (data) mz_free(data);
        return true;
    }
    bool empty = true;
    try {
        const json doc = json::parse(std::string(static_cast<const char*>(data), size));
        empty = documentJsonIsEmpty(doc);
    } catch (...) {
        empty = true;  // unreadable document.json — not worth recovering
    }
    mz_free(data);
    return empty;
}

static long long fileMtimeMs(const fs::path& p) {
    std::error_code ec;
    const auto t = fs::last_write_time(p, ec);
    if (ec) return 0;
    return std::chrono::duration_cast<std::chrono::milliseconds>(t.time_since_epoch())
        .count();
}

void Autosave::start(const std::string& baseDir, const std::atomic<int>* engineState) {
    if (!chudware_ipc::privateDir(baseDir)) {
        log().line("autosave: refusing non-private state directory");
        recoveryDone_ = true;
        return;
    }
    baseDir_ = baseDir;
    orphanDir_ = baseDir_ + "/orphaned";
    engineState_ = engineState;
    if (const char* v = std::getenv("CHUDWARE_AUTOSAVE_INTERVAL_S"); v && *v)
        intervalS_ = std::atof(v);
    const long long startMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                                  std::chrono::system_clock::now().time_since_epoch())
                                  .count();
    char sid[64];
    std::snprintf(sid, sizeof sid, "%015lld-%ld", startMs, (long)CHUDWARE_GETPID());
    sessionDir_ = baseDir_ + "/" + sid;
    thread_ = std::thread([this] { loop(); });
}

void Autosave::stop() {
    stopping_ = true;
    cv_.notify_all();
    if (thread_.joinable()) thread_.join();
    // Clean shutdown DISCARDS unsaved work — it is not a crash, so nothing is
    // preserved for recovery (the front-line guard is the GUI's "unsaved changes
    // on quit" warning). Removing this session's dir is the discard: the next
    // start finds no prior session and offers no recovery. A hard kill skips this
    // whole path — a surviving session dir IS the crash-recovery signal.
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
    if (!chudware_ipc::privateDir(baseDir_)) return;

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

    // A surviving prior-session dir means that daemon CRASHED (a clean shutdown
    // removes its own session dir). Crash recovery is the ONLY path that feeds the
    // recovery tier now — clean shutdown and doc-GC both discard. We do NOT
    // auto-open the survivors (that silently resurrected zombie parts); instead we
    // register each into the orphan tier so a client restores it deliberately via
    // recovery_list -> recovery_open (GUI "Recover?" prompt / recover_parts tool).
    const std::string dir = baseDir_ + "/" + latest;
    if (!chudware_ipc::privateDir(orphanDir_)) return;
    const long long nowMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::system_clock::now().time_since_epoch())
                                .count();
    int recovered = 0;
    for (const auto& e : fs::directory_iterator(dir, ec)) {
        if (e.path().extension() != ".step+") continue;
        // Empty/default docs (fresh AI parts with no features) are not worth
        // recovering — drop them instead of flooding the Recover? prompt.
        if (stepPlusFileIsEmpty(e.path().string())) {
            log().line("crash recovery: discarding empty doc %s",
                       e.path().filename().string().c_str());
            fs::remove(e.path(), ec);
            fs::remove(dir + "/" + e.path().stem().string() + ".json", ec);
            ec.clear();
            continue;
        }
        const std::string oldId = e.path().stem().string();
        std::string name = oldId;
        {
            std::ifstream in(dir + "/" + oldId + ".json");
            if (in) {
                try {
                    json m;
                    in >> m;
                    name = m.value("name", oldId);
                } catch (...) {
                }
            }
        }
        // stem = <ms>-<doc_id>: doc_id keeps it unique even within one ms, and the
        // orphan meta records the ORIGINAL id so a reconnecting agent's old handle
        // still resolves once recovery_open mints the new one.
        char stem[128];
        std::snprintf(stem, sizeof stem, "%015lld-%s", nowMs, oldId.c_str());
        std::lock_guard<std::mutex> lk(orphanMtx_);
        if (!chudware_ipc::privateCopy(e.path().string(), orphanDir_ + "/" + stem + ".step+")) {
            log().line("crash recovery: private copy %s -> orphan failed", oldId.c_str());
            continue;
        }
        if (!chudware_ipc::privateWrite(orphanDir_ + "/" + stem + ".json",
                    json{{"doc_id", oldId},
                         {"name", name},
                         {"retired_ms", nowMs},
                         {"from_crash", true}}.dump())) {
            fs::remove(orphanDir_ + "/" + stem + ".step+", ec);
            continue;
        }
        ++recovered;
    }
    fs::remove_all(dir, ec);  // consumed into the orphan tier
    recoverDir_.clear();
    if (recovered) {
        sweepOrphans();
        log().line("crash recovery: registered %d doc(s) from session %s into the "
                   "recovery tier (awaiting recovery_open)",
                   recovered, latest.c_str());
    }
    // Also scrub any pre-existing empty orphans left by older builds.
    (void)recoveryList();
}

void Autosave::saveDirtyDocs() {
    json docs;
    std::string err;
    if (!core::cpp_core().call("doc_list", json::object(), docs, err)) return;
    if (!docs.is_object() || !docs.contains("docs")) return;

    std::error_code ec;
    for (const auto& d : docs["docs"]) {
        if (!d.is_object() || !d.value("dirty", false)) continue;
        // Never autosave a blank/default doc — that is what filled the recovery
        // tier with dozens of empty "AI part" tabs — nor a pristine import of a file
        // that still exists (docListRowIsReproducible): the source IS its backup.
        if (d.value("empty", false) || docListRowIsReproducible(d)) continue;
        const std::string id = d.value("doc_id", std::string());
        if (id.empty()) continue;
        // Internal throwaway (the agent preflight shadow clone): not user work, so it
        // gets no artifact on disk. This is the CRASH half of the guard — if the daemon
        // dies hard, a clone that was never autosaved cannot be crash-registered into
        // the orphan tier and offered back as a recoverable `<part>_pf` document.
        if (isInternalDoc(id)) continue;
        const long rev = d.contains("revision") && d["revision"].is_number()
                             ? d["revision"].get<long>()
                             : -1;
        {
            std::lock_guard<std::mutex> lk(revMtx_);
            auto it = lastSavedRev_.find(id);
            if (it != lastSavedRev_.end() && rev >= 0 && it->second == rev)
                continue;  // unchanged since last autosave — skip the full serialize
        }
        if (!chudware_ipc::privateDir(sessionDir_)) {
            log().line("autosave: cannot create private session directory");
            return;
        }
        json result;
        const std::string path = sessionDir_ + "/" + id + ".step+";
        if (core::cpp_core().call("doc_save",
                                  json{{"doc_id", id}, {"path", path}, {"preserve_path", true}},
                                  result, err)) {
            if (rev >= 0) {
                std::lock_guard<std::mutex> lk(revMtx_);
                lastSavedRev_[id] = rev;
            }
            // Name sidecar: crash recovery registers this .step+ into the orphan
            // tier WITHOUT opening it, so the human-readable name has to travel
            // beside the file (the .step+ stem is the doc_id).
            if (!chudware_ipc::privateWrite(sessionDir_ + "/" + id + ".json",
                    json{{"name", d.value("name", id)}}.dump()))
                log().line("autosave: private name sidecar write failed for %s", id.c_str());
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
    fs::remove(sessionDir_ + "/" + docId + ".json", ec);  // name sidecar
    { std::lock_guard<std::mutex> lk(revMtx_); lastSavedRev_.erase(docId); }
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

void Autosave::noteRecoveredRemap(const std::string& oldId, const std::string& newId) {
    if (oldId.empty() || newId.empty() || oldId == newId) return;
    std::lock_guard<std::mutex> lk(remapMtx_);
    idRemap_[oldId] = newId;
}

bool Autosave::retireDoc(const std::string& docId, const std::string& name) {
    if (docId.empty()) return false;
    // Internal throwaway (the agent preflight shadow clone) — never recovery-worthy.
    // Mirrors the empty-doc branch below: purge any artifact and report success so
    // gcCloseDoc still closes the doc, it just leaves nothing behind to recover.
    if (isInternalDoc(docId)) {
        log().line("doc-gc: not retiring internal doc %s ('%s')", docId.c_str(), name.c_str());
        std::error_code ec0;
        fs::remove(sessionDir_ + "/" + docId + ".step+", ec0);
        fs::remove(sessionDir_ + "/" + docId + ".json", ec0);
        { std::lock_guard<std::mutex> lk(revMtx_); lastSavedRev_.erase(docId); }
        return true;
    }
    // Peek live doc_list for the empty / reproducible flags before writing an orphan
    // artifact.
    {
        json docs;
        std::string err;
        if (core::cpp_core().call("doc_list", json::object(), docs, err) &&
            docs.is_object() && docs.contains("docs")) {
            for (const auto& d : docs["docs"]) {
                if (!d.is_object() || d.value("doc_id", std::string()) != docId) continue;
                const bool empty = d.value("empty", false);
                if (empty || docListRowIsReproducible(d)) {
                    log().line("doc-gc: not retiring %s doc %s ('%s')",
                               empty ? "empty" : "reproducible import", docId.c_str(),
                               name.c_str());
                    // Still purge any prior session autosave for this id.
                    std::error_code ec2;
                    fs::remove(sessionDir_ + "/" + docId + ".step+", ec2);
                    fs::remove(sessionDir_ + "/" + docId + ".json", ec2);
                    { std::lock_guard<std::mutex> lk(revMtx_); lastSavedRev_.erase(docId); }
                    return true;  // "success" so gcCloseDoc proceeds to close
                }
                break;
            }
        }
    }
    std::error_code ec;
    if (!chudware_ipc::privateDir(orphanDir_)) return false;
    const long long nowMs = std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::system_clock::now().time_since_epoch())
                                .count();
    char stem[128];
    std::snprintf(stem, sizeof stem, "%015lld-%s", nowMs, docId.c_str());
    const std::string base = orphanDir_ + "/" + stem;
    json result;
    std::string err;
    if (!core::cpp_core().call("doc_save",
                               json{{"doc_id", docId}, {"path", base + ".step+"},
                                    {"preserve_path", true}},
                               result, err)) {
        log().line("doc-gc: retire doc_save(%s) failed: %s", docId.c_str(), err.c_str());
        return false;
    }
    {
        std::lock_guard<std::mutex> lk(orphanMtx_);
        if (!chudware_ipc::privateWrite(base + ".json",
                json{{"doc_id", docId}, {"name", name}, {"retired_ms", nowMs}}.dump())) {
            fs::remove(base + ".step+", ec);
            return false;
        }
    }
    fs::remove(sessionDir_ + "/" + docId + ".step+", ec);  // session copy is now redundant
    fs::remove(sessionDir_ + "/" + docId + ".json", ec);    // name sidecar
    { std::lock_guard<std::mutex> lk(revMtx_); lastSavedRev_.erase(docId); }
    sweepOrphans();
    return true;
}

json Autosave::orphanMeta(const std::string& id) {
    if (id.empty() || id.find('/') != std::string::npos ||
        id.find("..") != std::string::npos)
        return json::object();
    std::lock_guard<std::mutex> lk(orphanMtx_);
    std::ifstream in(orphanDir_ + "/" + id + ".json");
    if (!in) return json::object();
    try {
        json m;
        in >> m;
        return m.is_object() ? m : json::object();
    } catch (...) {
        return json::object();
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
        // Auto-purge empty/default orphans so the Recover? prompt never offers them.
        if (stepPlusFileIsEmpty(p.string())) {
            std::lock_guard<std::mutex> lk(orphanMtx_);
            fs::remove(p, ec);
            fs::remove(orphanDir_ + "/" + stem + ".json", ec);
            log().line("recovery: discarded empty orphan %s", stem.c_str());
            continue;
        }
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
    // Longer than the old 20, but deliberately NOT unbounded: the GUI "Recover"
    // button loads every listed entry synchronously (attach + tree + mesh per
    // doc), so a very deep tier would reproduce the original "loading a pile of
    // garbage freezes the app" symptom. 50 keeps a generous disk history while
    // capping a worst-case Recover-all. Raise CHUDWARE_ORPHAN_KEEP if you accept that.
    int keep = 50;
    if (const char* v = std::getenv("CHUDWARE_ORPHAN_KEEP"); v && *v)
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
