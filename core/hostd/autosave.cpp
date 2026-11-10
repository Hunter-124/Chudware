#include "autosave.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <exception>
#include <filesystem>
#include <fstream>
#include <vector>

#ifdef _WIN32
#include <process.h>
#define CHUDWARE_GETPID _getpid
#else
#include <sys/stat.h>
#include <unistd.h>
#define CHUDWARE_GETPID getpid
#endif

#include "../../services/worker/cpp_core.h"
#include "../../services/ipc/ipc_private.h"
#include "../doc/native_policy.h"
#include "hostd_log.h"

#include <miniz.h>

namespace hostd {
namespace fs = std::filesystem;
using nlohmann::json;
using chudware::doc::native_policy::path_utf8;

namespace {
long long nowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch()).count();
}

std::string stringField(const json& row, const char* key, const std::string& fallback = {}) {
    auto it = row.find(key);
    return it != row.end() && it->is_string() ? it->get<std::string>() : fallback;
}

long numberField(const json& row, const char* key, long fallback = -1) {
    auto it = row.find(key);
    return it != row.end() && it->is_number_integer() ? it->get<long>() : fallback;
}

bool validId(const std::string& id) {
    return !id.empty() && id.find_first_of("/\\") == std::string::npos &&
           id.find("..") == std::string::npos;
}

json readMeta(const std::string& path, bool* readable = nullptr) {
    if (readable) *readable = true;
    std::error_code ec;
    const bool present = fs::exists(fs::u8path(path), ec);
    if (ec) { if (readable) *readable = false; return json::object(); }
    if (!present) return json::object();  // older snapshots can lack a sidecar
    std::ifstream in(fs::u8path(path));
    if (!in) { if (readable) *readable = false; return json::object(); }
    try {
        json result;
        in >> result;
        if (result.is_object()) return result;
    } catch (...) {}
    if (readable) *readable = false;
    return json::object();
}

bool durableMeta(const std::string& path, const json& meta) {
    const std::string stage = chudware_ipc::privateStageName(path);
    FILE* file = chudware_ipc::privateFile(stage, false, true);
    if (!file) return false;
    const std::string text = meta.dump();
    bool ok = std::fwrite(text.data(), 1, text.size(), file) == text.size();
    if (ok) ok = chudware_ipc::syncPrivateFile(file);
    ok = std::fclose(file) == 0 && ok;
    if (ok) ok = chudware_ipc::publishPrivateFile(stage, path);
    if (!ok) { std::error_code ec; fs::remove(stage, ec); }
    return ok;
}

bool syncExisting(const std::string& path) {
#ifdef _WIN32
    FILE* file = _wfopen(fs::u8path(path).c_str(), L"rb+");
#else
    FILE* file = std::fopen(path.c_str(), "rb+");
#endif
    if (!file) return false;
    const bool ok = chudware_ipc::syncPrivateFile(file);
    return std::fclose(file) == 0 && ok;
}

// An unreadable archive is not an empty document. Keep it quarantined for
// repair rather than ever interpreting an I/O or parse error as permission to delete.
bool validSnapshot(const std::string& path) {
    mz_zip_archive zip;
    std::memset(&zip, 0, sizeof(zip));
#ifdef _WIN32
    FILE* file = _wfopen(fs::u8path(path).c_str(), L"rb");
#else
    FILE* file = std::fopen(path.c_str(), "rb");
#endif
    if (!file) return false;
    if (!mz_zip_reader_init_cfile(&zip, file, 0, 0)) {
        std::fclose(file);
        return false;
    }
    size_t size = 0;
    void* data = mz_zip_reader_extract_file_to_heap(&zip, "document.json", &size, 0);
    mz_zip_reader_end(&zip);
    std::fclose(file);
    if (!data || !size) { if (data) mz_free(data); return false; }
    bool ok = false;
    try { ok = json::parse(std::string(static_cast<const char*>(data), size)).is_object(); }
    catch (...) {}
    mz_free(data);
    return ok;
}

bool quarantine(const fs::path& file) {
    const fs::path dir = file.parent_path() / "quarantine";
    if (!chudware_ipc::privateDir(path_utf8(dir))) return false;
    std::error_code ec;
    const fs::path dest = dir / file.filename();
    if (fs::exists(dest, ec) || ec) return false;
    fs::rename(file, dest, ec);
    if (ec) return false;
    const fs::path sidecar = file.parent_path() / (file.stem().string() + ".json");
    fs::rename(sidecar, dir / sidecar.filename(), ec);
    chudware_ipc::syncParentDirectory(path_utf8(dest));
    chudware_ipc::syncParentDirectory(path_utf8(file));
    log().line("recovery: quarantined unreadable snapshot %s", path_utf8(file).c_str());
    return true;
}

json currentStamp(const std::string& path) {
#ifdef _WIN32
    HANDLE h = CreateFileW(fs::u8path(path).c_str(), FILE_READ_ATTRIBUTES,
                           FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           nullptr, OPEN_EXISTING, 0, nullptr);
    if (h == INVALID_HANDLE_VALUE) return nullptr;
    BY_HANDLE_FILE_INFORMATION info{};
    const bool ok = GetFileInformationByHandle(h, &info) != 0;
    CloseHandle(h);
    if (!ok || (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) return nullptr;
    const unsigned long long ticks = (static_cast<unsigned long long>(info.ftLastWriteTime.dwHighDateTime) << 32) |
                                     info.ftLastWriteTime.dwLowDateTime;
    return json{{"size", (static_cast<unsigned long long>(info.nFileSizeHigh) << 32) | info.nFileSizeLow},
                {"mtime_ns", static_cast<long long>(ticks - 116444736000000000ULL) * 100},
                {"ino", (static_cast<unsigned long long>(info.nFileIndexHigh) << 32) | info.nFileIndexLow}};
#else
    struct stat st{};
    if (::stat(path.c_str(), &st) != 0 || !S_ISREG(st.st_mode)) return nullptr;
#if defined(__APPLE__)
    const long long mtime = static_cast<long long>(st.st_mtimespec.tv_sec) * 1000000000LL + st.st_mtimespec.tv_nsec;
#else
    const long long mtime = static_cast<long long>(st.st_mtim.tv_sec) * 1000000000LL + st.st_mtim.tv_nsec;
#endif
    return json{{"size", static_cast<unsigned long long>(st.st_size)},
                {"mtime_ns", mtime}, {"ino", static_cast<unsigned long long>(st.st_ino)}};
#endif
}

bool replaceFile(const std::string& stage, const std::string& target, const json& expected) {
#ifdef _WIN32
    if (!syncExisting(stage) || currentStamp(target) != expected) return false;
    return MoveFileExW(fs::u8path(stage).c_str(), fs::u8path(target).c_str(),
                       MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH) != 0;
#else
    struct stat original{};
    if (::stat(target.c_str(), &original) != 0 ||
        ::chmod(stage.c_str(), original.st_mode & 07777) != 0 ||
        !syncExisting(stage) || currentStamp(target) != expected ||
        ::rename(stage.c_str(), target.c_str()) != 0) return false;
    return chudware_ipc::syncParentDirectory(target);
#endif
}
}  // namespace

bool Autosave::start(const std::string& baseDir, const std::atomic<int>* engineState) {
    if (!chudware_ipc::privateDir(baseDir)) {
        log().line("autosave: refusing non-private state directory");
        recoveryDone_ = true;
        cv_.notify_all();
        return false;
    }
    baseDir_ = baseDir;
    orphanDir_ = baseDir_ + "/orphaned";
    engineState_ = engineState;
    if (const char* v = std::getenv("CHUDWARE_AUTOSAVE_INTERVAL_S"); v && *v)
        intervalS_ = std::atof(v);
    char sid[64];
    std::snprintf(sid, sizeof sid, "%015lld-%ld", nowMs(), (long)CHUDWARE_GETPID());
    sessionDir_ = baseDir_ + "/" + sid;
    thread_ = std::thread([this] { loop(); });
    return true;
}

void Autosave::waitForRecovery() {
    std::unique_lock<std::mutex> lk(mtx_);
    cv_.wait(lk, [this] { return recoveryDone_.load() || stopping_.load(); });
}

bool Autosave::recoveryAvailable() const {
    return !orphanDir_.empty() && chudware_ipc::privateDir(orphanDir_);
}

void Autosave::stop(bool keepForRecovery) {
    stopping_ = true;
    cv_.notify_all();
    if (thread_.joinable()) thread_.join();
    if (engineState_ && engineState_->load() == 1 && !sessionDir_.empty()) {
        // A failed capture is not an up-to-date snapshot, so retry briefly (a capture can
        // lose a race with in-flight work). The retry is bounded: a document that can never
        // be captured used to hold an idle-exited daemon alive forever. After the bound the
        // earlier snapshots in the session dir are still migrated for recovery below.
        constexpr int kMaxShutdownPasses = 10;
        bool saved = false;
        for (int pass = 0; pass < kMaxShutdownPasses && !(saved = saveDirtyDocs(pass == 0)); ++pass)
            std::this_thread::sleep_for(std::chrono::seconds(1));
        if (!saved)
            log().line("autosave: final snapshot still failing after %d passes — exiting "
                       "with the last good snapshots", kMaxShutdownPasses);
        migrateSession(sessionDir_);
    }
    log().line("autosave: %s shutdown preserved non-discarded work",
               keepForRecovery ? "external" : "clean");
}

json Autosave::idRemap() {
    std::lock_guard<std::mutex> lk(remapMtx_);
    return idRemap_;
}

void Autosave::loop() {
    while (!stopping_) {
        const int state = engineState_ ? engineState_->load() : 0;
        if (state == 1) break;
        if (state == 2) { recoveryDone_ = true; cv_.notify_all(); return; }
        std::unique_lock<std::mutex> lk(mtx_);
        cv_.wait_for(lk, std::chrono::milliseconds(200));
    }
    if (stopping_) return;
    try {
        recoverPriorSession();
        autoRestoreOrphans();
    } catch (const std::exception& e) {
        recoveryIncomplete_ = true;
        if (!recoveryFailureLogged_.exchange(true))
            log().line("recovery: migration failed; sources retained: %s", e.what());
    } catch (...) {
        recoveryIncomplete_ = true;
        if (!recoveryFailureLogged_.exchange(true))
            log().line("recovery: migration failed; sources retained");
    }
    recoveryDone_ = true;
    cv_.notify_all();
    if (intervalS_ <= 0) return;
    bool ok = true;
    while (!stopping_) {
        std::unique_lock<std::mutex> lk(mtx_);
        cv_.wait_for(lk, std::chrono::duration<double>(ok ? intervalS_ : 1.0),
                     [this] { return stopping_.load(); });
        lk.unlock();
        if (!stopping_) ok = saveDirtyDocs();
    }
}

void Autosave::recoverPriorSession() {
    std::lock_guard<std::mutex> pass(migrationMtx_);
    std::error_code ec;
    std::vector<fs::path> sessions;
    for (const auto& entry : fs::directory_iterator(baseDir_, ec)) {
        if (entry.is_directory() && entry.path().filename() != "orphaned" &&
            entry.path() != fs::u8path(sessionDir_)) sessions.push_back(entry.path());
    }
    std::sort(sessions.begin(), sessions.end());
    bool complete = !ec;
    for (const auto& dir : sessions)
        if (!migrateSession(path_utf8(dir))) complete = false;
    recoveryIncomplete_ = !complete;
}

bool Autosave::migrateSession(const std::string& dir) {
    if (!chudware_ipc::privateDir(orphanDir_)) return false;
    std::lock_guard<std::mutex> transaction(recoveryTxnMtx_);
    std::lock_guard<std::mutex> lk(orphanMtx_);
    std::error_code ec;
    int migrated = 0;
    bool complete = true;
    for (const auto& entry : fs::directory_iterator(dir, ec)) {
        if (entry.path().extension() != ".step+") continue;
        const std::string source = path_utf8(entry.path());
        if (!validSnapshot(source)) {
            if (!quarantine(entry.path())) complete = false;
            continue;
        }
        const std::string oldStem = entry.path().stem().string();
        const std::string oldMeta = path_utf8(entry.path().parent_path() / (oldStem + ".json"));
        bool metaReadable = false;
        json meta = readMeta(oldMeta, &metaReadable);
        if (!metaReadable) {
            if (!quarantine(entry.path())) complete = false;
            continue;
        }
        const std::string oldId = stringField(meta, "doc_id", oldStem);
        const std::string stem = std::to_string(nowMs()) + "-" +
            std::to_string(CHUDWARE_GETPID()) + "-" +
            std::to_string(snapshotSerial_.fetch_add(1, std::memory_order_relaxed));
        const std::string base = orphanDir_ + "/" + stem;
        meta["doc_id"] = oldId;
        meta["name"] = stringField(meta, "name", oldId);
        meta["path"] = stringField(meta, "path");
        meta["origin_kind"] = stringField(meta, "origin_kind", "unknown");
        if (!meta.contains("human_dirty") || !meta["human_dirty"].is_boolean()) meta["human_dirty"] = true;
        if (!meta.contains("revision")) meta["revision"] = -1;
        if (!meta.contains("saved_revision")) meta["saved_revision"] = -1;
        if (!meta.contains("captured_at")) meta["captured_at"] = nowMs();
        if (!durableMeta(base + ".json", meta) ||
            !chudware_ipc::privateCopy(source, base + ".step+")) {
            log().line("crash recovery: preserving source %s after migration failure", source.c_str());
            complete = false;
            continue;
        }
        // Both replacement files have been flushed and published. Failed
        // source removal merely leaves a duplicate, never loses the source.
        fs::remove(entry.path(), ec);
        if (!ec) fs::remove(fs::u8path(oldMeta), ec);
        chudware_ipc::syncParentDirectory(source);
        ++migrated;
    }
    if (ec) complete = false;
    fs::remove(fs::u8path(dir), ec);  // only if completely empty
    if (migrated) log().line("recovery: migrated %d snapshot(s) from %s", migrated, dir.c_str());
    return complete;
}

void Autosave::autoRestoreOrphans() {
    std::lock_guard<std::mutex> transaction(recoveryTxnMtx_);
    std::lock_guard<std::mutex> lk(orphanMtx_);
    if (!recoveryAvailable()) {
        log().line("recovery: private recovery directory unavailable; sources retained");
        return;
    }
    std::error_code ec;
    struct Candidate { fs::path file; json meta; long long captured; long revision; long metadataRevision; bool readable; };
    std::vector<Candidate> files;
    for (const auto& entry : fs::directory_iterator(orphanDir_, ec)) {
        if (entry.path().extension() != ".step+") continue;
        bool readable = false;
        json meta = readMeta(path_utf8(entry.path().parent_path() /
                                       (entry.path().stem().string() + ".json")), &readable);
        const long long captured = meta.contains("captured_at") && meta["captured_at"].is_number_integer()
                                     ? meta["captured_at"].get<long long>() : 0;
        const long revision = numberField(meta, "revision");
        const long metadataRevision = numberField(meta, "metadata_revision");
        files.push_back({entry.path(), std::move(meta), captured, revision, metadataRevision, readable});
    }
    std::sort(files.begin(), files.end(), [](const Candidate& a, const Candidate& b) {
        if (a.captured != b.captured) return a.captured > b.captured;
        if (a.revision != b.revision) return a.revision > b.revision;
        if (a.metadataRevision != b.metadataRevision) return a.metadataRevision > b.metadataRevision;
        return a.file > b.file;
    });
    for (const auto& candidate : files) {
        const fs::path& file = candidate.file;
        const std::string filePath = path_utf8(file);
        if (!candidate.readable || !validSnapshot(filePath)) { quarantine(file); continue; }
        const std::string sidecar = path_utf8(file.parent_path() / (file.stem().string() + ".json"));
        const json& meta = candidate.meta;
        if (!meta.contains("human_dirty") || !meta["human_dirty"].is_boolean() ||
            meta["human_dirty"].get<bool>()) continue;
        const std::string target = stringField(meta, "path");
        const json base = meta.value("file_stamp", json());
        if (target.empty() || !base.is_object() || currentStamp(target) != base) continue;

        json opened, saved, closed;
        std::string err;
        if (!core::cpp_core().call("doc_open", json{{"path", filePath},
                                                   {"detach_path", true}, {"recovered", true}}, opened, err)) {
            log().line("recovery: automatic restore open failed for %s: %s", filePath.c_str(), err.c_str());
            continue;
        }
        const std::string docId = stringField(opened, "doc_id");
        const std::string stage = chudware_ipc::privateStageName(target);
        bool ok = !docId.empty() && core::cpp_core().call("doc_save", json{{"doc_id", docId},
                          {"path", stage}, {"preserve_path", true}}, saved, err);
        if (!docId.empty() && core::cpp_core().call(
                "doc_close", json{{"doc_id", docId}}, closed, err))
            log().line("doc removal: doc=%s client=0 kind=daemon method=doc_close reason=auto_restore dirty=true preservation=orphan",
                       docId.c_str());
        if (ok) ok = replaceFile(stage, target, base);
        if (ok) {
            if (fs::remove(file, ec) && !ec) fs::remove(fs::u8path(sidecar), ec);
            chudware_ipc::syncParentDirectory(sidecar);
            log().line("recovery: automatically restored AI work to %s", target.c_str());
        } else {
            fs::remove(fs::u8path(stage), ec);
            log().line("recovery: retained AI snapshot %s (automatic restore failed or file changed)",
                       filePath.c_str());
        }
    }
}

bool Autosave::docRow(const std::string& id, json& row) {
    json docs;
    std::string err;
    if (!core::cpp_core().call("doc_list", json::object(), docs, err) ||
        !docs.is_object() || !docs.contains("docs") || !docs["docs"].is_array()) return false;
    if (annotate_) annotate_(docs);
    for (const auto& d : docs["docs"]) {
        if (d.is_object() && stringField(d, "doc_id") == id) { row = d; return true; }
    }
    return false;
}

bool Autosave::saveDirtyDocs(bool force) try {
    json docs;
    std::string err;
    if (!core::cpp_core().call("doc_list", json::object(), docs, err) ||
        !docs.is_object() || !docs.contains("docs") || !docs["docs"].is_array()) {
        if (!scanFailureLogged_.exchange(true))
            log().line("autosave: cannot enumerate documents: %s", err.c_str());
        return false;
    }
    if (annotate_) annotate_(docs);
    bool ok = true;
    for (const auto& d : docs["docs"]) {
        if (!d.is_object()) continue;
        const std::string id = stringField(d, "doc_id");
        if (id.empty() || isInternalDoc(id)) continue;
        const bool humanDirty = d.value("human_dirty", false);
        if (!d.value("dirty", false) && !humanDirty) continue;
        const long rev = numberField(d, "revision");
        const long metaRev = numberField(d, "metadata_revision");
        if (d.value("empty", false) && rev <= 0 && metaRev <= 0 && !humanDirty) continue;
        {
            std::lock_guard<std::mutex> lk(revMtx_);
            auto it = lastSavedRev_.find(id);
            const auto path = lastSavedPath_.find(id);
            std::error_code ec;
            if (!force && it != lastSavedRev_.end() && path != lastSavedPath_.end() &&
                rev >= 0 && metaRev >= 0 && it->second == std::make_pair(rev, metaRev) &&
                fs::is_regular_file(fs::u8path(path->second), ec)) continue;
        }
        try {
            if (!snapshotDoc(d)) ok = false;
        } catch (const std::exception& e) {
            snapshotFailure(id, rev, metaRev, e.what());
            ok = false;
        } catch (...) {
            snapshotFailure(id, rev, metaRev, "unknown snapshot error");
            ok = false;
        }
    }
    scanFailureLogged_ = false;
    return ok;
} catch (const std::exception& e) {
    if (!scanFailureLogged_.exchange(true))
        log().line("autosave: document scan failed: %s", e.what());
    return false;
} catch (...) {
    if (!scanFailureLogged_.exchange(true))
        log().line("autosave: document scan failed");
    return false;
}

void Autosave::snapshotFailure(const std::string& id, long revision, long metadataRevision,
                               const std::string& message) {
    std::lock_guard<std::mutex> lk(revMtx_);
    // A failed attempt must not certify a previous snapshot as current.
    lastSavedRev_.erase(id);
    lastSavedPath_.erase(id);
    auto it = failedRev_.find(id);
    if (it == failedRev_.end() || it->second != std::make_pair(revision, metadataRevision)) {
        failedRev_[id] = {revision, metadataRevision};
        log().line("autosave: snapshot %s revision %ld/%ld failed: %s",
                   id.c_str(), revision, metadataRevision, message.c_str());
    }
}

bool Autosave::capture(const json& row, const std::string& directory,
                       long& revision, long& metadataRevision, std::string& published) {
    struct Context {
        Autosave* self;
        const json& row;
        const std::string& directory;
        long& revision;
        long& metadataRevision;
        std::string& published;
        bool ok = false;
    } context{this, row, directory, revision, metadataRevision, published};
    const auto work = [state = &context] {
        state->ok = state->self->captureGuarded(state->row, state->directory,
                     state->revision, state->metadataRevision, state->published);
    };
    if (captureGuard_) captureGuard_(stringField(row, "doc_id"), work);
    else work();
    return context.ok;
}

bool Autosave::captureGuarded(const json& initial, const std::string& directory,
                       long& revision, long& metadataRevision, std::string& published) {
    const std::string id = stringField(initial, "doc_id");
    json row;
    if (!docRow(id, row)) {
        snapshotFailure(id, numberField(initial, "revision"), numberField(initial, "metadata_revision"),
                        "document state unavailable");
        return false;
    }
    revision = numberField(row, "revision");
    metadataRevision = numberField(row, "metadata_revision");
    const std::string boundPath = stringField(row, "path");
    // A real save may have completed while this capture waited for the guard.
    // Do not publish a stale recovery artifact after that save's prune.
    if (!row.value("dirty", false) && !row.value("human_dirty", false) &&
        !boundPath.empty() && !isAutosavePath(boundPath)) return false;
    if (!validId(id) || !chudware_ipc::privateDir(directory)) {
        snapshotFailure(id, revision, metadataRevision, "invalid id or private directory unavailable");
        return false;
    }
    const std::string stem = std::to_string(nowMs()) + "-" + std::to_string(CHUDWARE_GETPID()) +
        "-" + std::to_string(snapshotSerial_.fetch_add(1, std::memory_order_relaxed));
    const std::string base = directory + "/" + stem;
    const std::string stage = directory + "/." + stem + ".tmp";
    json result;
    std::string err;
    if (!core::cpp_core().call("doc_save", json{{"doc_id", id}, {"path", stage},
                                                {"preserve_path", true}}, result, err)) {
        snapshotFailure(id, revision, metadataRevision, err);
        return false;
    }
    revision = numberField(result, "saved_revision", numberField(result, "revision", revision));
    metadataRevision = numberField(result, "metadata_revision", metadataRevision);
    json latest;
    const bool haveLatest = docRow(id, latest);
    const bool humanDirty = row.value("human_dirty", true) || !haveLatest || latest.value("human_dirty", true);
    std::string path = stringField(row, "path");
    json stamp = row.value("file_stamp", json());
    if (path.empty()) {
        std::lock_guard<std::mutex> lk(revMtx_);
        auto it = recoveredSource_.find(id);
        if (it != recoveredSource_.end()) {
            path = stringField(it->second.meta, "path");
            stamp = it->second.meta.value("file_stamp", json());
        }
        if (!haveLatest || !stringField(latest, "path").empty()) stamp = nullptr;
    } else if (!haveLatest || stringField(latest, "path") != path) {
        stamp = nullptr;
    }
    json meta{{"doc_id", id}, {"name", stringField(row, "name", id)}, {"path", path},
              {"origin_kind", stringField(row, "origin_kind", "unknown")},
              {"human_dirty", humanDirty}, {"revision", revision},
              {"metadata_revision", metadataRevision},
              {"saved_revision", numberField(row, "saved_revision")},
              {"captured_at", nowMs()}, {"file_stamp", stamp},
              {"session_id", path_utf8(fs::u8path(sessionDir_).filename())}};
    std::unique_lock<std::mutex> orphanLock(orphanMtx_, std::defer_lock);
    if (directory == orphanDir_) orphanLock.lock();
    if (!syncExisting(stage) || !durableMeta(base + ".json", meta) ||
        !chudware_ipc::publishPrivateFile(stage, base + ".step+")) {
        snapshotFailure(id, revision, metadataRevision, "could not durably publish snapshot and metadata");
        return false;
    }
    published = base;
    return true;
}

bool Autosave::snapshotDoc(const json& row) {
    const std::string id = stringField(row, "doc_id");
    unsigned long long epoch;
    { std::lock_guard<std::mutex> lk(revMtx_); epoch = snapshotEpoch_[id]; }
    long rev, metaRev;
    std::string base;
    if (!capture(row, sessionDir_, rev, metaRev, base)) return false;
    std::lock_guard<std::mutex> lk(revMtx_);
    auto prior = lastSavedRev_.find(id);
    if (snapshotEpoch_[id] != epoch ||
        (prior != lastSavedRev_.end() && prior->second > std::make_pair(rev, metaRev))) {
        // Remove only if a successful save or a newer committed snapshot
        // covers both dimensions of this capture.
        const auto cutoff = savedCutoff_.find(id);
        const bool saved = cutoff != savedCutoff_.end() && rev >= 0 && metaRev >= 0 &&
                           rev <= cutoff->second.first && metaRev <= cutoff->second.second;
        const auto priorPath = lastSavedPath_.find(id);
        std::error_code existsError;
        const bool newer = prior != lastSavedRev_.end() && priorPath != lastSavedPath_.end() &&
                           rev >= 0 && metaRev >= 0 && rev <= prior->second.first &&
                           metaRev <= prior->second.second &&
                           fs::is_regular_file(fs::u8path(priorPath->second), existsError) &&
                           validSnapshot(priorPath->second);
        if (saved || newer) {
            std::error_code ec;
            if (fs::remove(base + ".step+", ec) && !ec) fs::remove(base + ".json", ec);
        }
        return false;
    }
    lastSavedRev_[id] = {rev, metaRev};
    lastSavedPath_[id] = base + ".step+";
    failedRev_.erase(id);
    // Only a committed newer snapshot can supersede an older session copy.
    std::error_code ec;
    for (const auto& entry : fs::directory_iterator(sessionDir_, ec)) {
        if (entry.path().extension() != ".step+" || entry.path() == fs::u8path(base + ".step+")) continue;
        const std::string sidecar = path_utf8(entry.path().parent_path() / (entry.path().stem().string() + ".json"));
        const json old = readMeta(sidecar);
        if (stringField(old, "doc_id") == id && numberField(old, "revision") <= rev &&
            numberField(old, "metadata_revision") <= metaRev) {
            if (fs::remove(entry.path(), ec) && !ec) fs::remove(fs::u8path(sidecar), ec);
        }
    }
    return true;
}

bool Autosave::snapshotNow(const std::string& docId) {
    json row;
    return docRow(docId, row) && snapshotDoc(row);
}

void Autosave::discardSessionSnapshot(const std::string& id) {
    std::lock_guard<std::mutex> lk(revMtx_);
    ++snapshotEpoch_[id];
    lastSavedRev_.erase(id);
    lastSavedPath_.erase(id);
    failedRev_.erase(id);
    recoveredSource_.erase(id);
    std::error_code ec;
    if (!fs::exists(fs::u8path(sessionDir_), ec) || ec ||
        !chudware_ipc::privateDir(sessionDir_)) return;
    for (const auto& entry : fs::directory_iterator(sessionDir_, ec)) {
        if (entry.path().extension() != ".step+") continue;
        const std::string sidecar = path_utf8(entry.path().parent_path() / (entry.path().stem().string() + ".json"));
        const json meta = readMeta(sidecar);
        if (stringField(meta, "doc_id", entry.path().stem().string()) != id) continue;
        if (fs::remove(entry.path(), ec) && !ec) fs::remove(fs::u8path(sidecar), ec);
    }
}

void Autosave::onDocClosed(const std::string& docId) {
    if (docId.empty()) return;
    std::string sourceId;
    {
        std::lock_guard<std::mutex> lk(revMtx_);
        const auto source = recoveredSource_.find(docId);
        if (source != recoveredSource_.end()) sourceId = source->second.orphanId;
    }
    discardSessionSnapshot(docId);
    if (!sourceId.empty()) {
        std::lock_guard<std::mutex> lk(orphanMtx_);
        if (recoveryAvailable()) supersedeOrphanLocked(sourceId);
    }
    std::lock_guard<std::mutex> lk(remapMtx_);
    for (auto it = idRemap_.begin(); it != idRemap_.end();) {
        if (it.value() == docId) it = idRemap_.erase(it);
        else ++it;
    }
}

void Autosave::onDocSaved(const std::string& id, long savedRevision, long metadataRevision) {
    if (id.empty() || savedRevision < 0) return;
    std::string sourceId;
    const auto prune = [&](const std::string& directory, bool sameSessionOnly) {
        std::error_code ec;
        for (const auto& entry : fs::directory_iterator(directory, ec)) {
            if (entry.path().extension() != ".step+") continue;
            if (sameSessionOnly && recoveryPins_.count(entry.path().stem().string())) continue;
            const std::string sidecar = path_utf8(entry.path().parent_path() / (entry.path().stem().string() + ".json"));
            const json meta = readMeta(sidecar);
            const long rev = numberField(meta, "revision");
            const long mrev = numberField(meta, "metadata_revision");
            if (stringField(meta, "doc_id", entry.path().stem().string()) != id || rev < 0 ||
                rev > savedRevision || metadataRevision < 0 || mrev < 0 || mrev > metadataRevision ||
                (sameSessionOnly && stringField(meta, "session_id") !=
                                    path_utf8(fs::u8path(sessionDir_).filename()))) continue;
            if (fs::remove(entry.path(), ec) && !ec) fs::remove(fs::u8path(sidecar), ec);
        }
    };
    {
        std::lock_guard<std::mutex> lk(revMtx_);
        ++snapshotEpoch_[id];
        if (metadataRevision >= 0) savedCutoff_[id] = {savedRevision, metadataRevision};
        const auto source = recoveredSource_.find(id);
        if (source != recoveredSource_.end() && source->second.revision >= 0 &&
            source->second.metadataRevision >= 0 &&
            savedRevision >= source->second.revision &&
            metadataRevision >= source->second.metadataRevision) {
            sourceId = source->second.orphanId;
            recoveredSource_.erase(source);
        }
        lastSavedRev_.erase(id);
        lastSavedPath_.erase(id);
        std::error_code ec;
        if (fs::exists(fs::u8path(sessionDir_), ec) && !ec &&
            chudware_ipc::privateDir(sessionDir_)) prune(sessionDir_, false);
    }
    // Never compare another daemon session's reused doc IDs to this save.
    std::lock_guard<std::mutex> lk(orphanMtx_);
    if (recoveryAvailable()) {
        prune(orphanDir_, true);
        supersedeOrphanLocked(sourceId);
    }
}

bool Autosave::retireDoc(const std::string& docId, const std::string& name,
                         long& revision, long& metadataRevision) {
    json row;
    if (docId.empty() || !docRow(docId, row)) return false;
    if (!row.contains("name")) row["name"] = name;
    std::string base;
    return capture(row, orphanDir_, revision, metadataRevision, base);
}

json Autosave::recoveryList(const std::string& scope) {
    if (recoveryIncomplete_.load()) {
        try {
            recoverPriorSession();
            if (!recoveryIncomplete_.load()) {
                autoRestoreOrphans();
                recoveryFailureLogged_ = false;
            }
        } catch (const std::exception& e) {
            recoveryIncomplete_ = true;
            if (!recoveryFailureLogged_.exchange(true))
                log().line("recovery: migration failed; sources retained: %s", e.what());
        } catch (...) {
            recoveryIncomplete_ = true;
            if (!recoveryFailureLogged_.exchange(true))
                log().line("recovery: migration failed; sources retained");
        }
    }
    if (recoveryIncomplete_.load()) return json{{"error", "recovery migration incomplete"}};
    std::lock_guard<std::mutex> transaction(recoveryTxnMtx_);
    std::lock_guard<std::mutex> lk(orphanMtx_);
    if (!recoveryAvailable())
        return json{{"error", "private recovery directory unavailable"}};
    json entries = json::array();
    std::error_code ec;
    std::vector<fs::path> files;
    for (const auto& entry : fs::directory_iterator(orphanDir_, ec))
        if (entry.path().extension() == ".step+") files.push_back(entry.path());
    if (ec) return json{{"error", "cannot read recovery directory"}};
    std::sort(files.begin(), files.end(), std::greater<>());
    for (const auto& file : files) {
        if (!validSnapshot(path_utf8(file))) {
            if (!quarantine(file)) return json{{"error", "cannot quarantine unreadable snapshot"}};
            continue;
        }
        const std::string id = file.stem().string();
        bool metaReadable = false;
        const json meta = readMeta(path_utf8(file.parent_path() / (id + ".json")), &metaReadable);
        if (!metaReadable) {
            if (!quarantine(file)) return json{{"error", "cannot quarantine unreadable metadata"}};
            continue;
        }
        const bool humanDirty = !meta.contains("human_dirty") || !meta["human_dirty"].is_boolean()
                                    || meta["human_dirty"].get<bool>();
        if ((scope == "ai" && humanDirty) || (scope == "human" && !humanDirty)) continue;
        const long long captured = meta.contains("captured_at") && meta["captured_at"].is_number_integer()
                                     ? meta["captured_at"].get<long long>() : 0;
        const std::string session = stringField(meta, "session_id");
        entries.push_back({{"id", id}, {"doc_id", stringField(meta, "doc_id", id)},
                           {"name", stringField(meta, "name", id)}, {"path", stringField(meta, "path")},
                           {"origin_kind", stringField(meta, "origin_kind", "unknown")},
                           {"human_dirty", humanDirty}, {"revision", numberField(meta, "revision")},
                           {"metadata_revision", numberField(meta, "metadata_revision")},
                           {"same_session", !session.empty() &&
                                session == path_utf8(fs::u8path(sessionDir_).filename())},
                           {"captured_at", captured}, {"mtime_ms", captured}});
    }
    return json{{"entries", entries}};
}

int Autosave::orphanCount() {
    std::lock_guard<std::mutex> lk(orphanMtx_);
    if (!recoveryAvailable()) return 0;
    int count = 0;
    std::error_code ec;
    for (const auto& entry : fs::directory_iterator(orphanDir_, ec))
        if (entry.path().extension() == ".step+") ++count;
    return count;
}

bool Autosave::recoverOrphan(const std::string& id,
    const std::function<bool(const std::string&, const json&)>& open) {
    if (!validId(id)) return false;
    std::lock_guard<std::mutex> transaction(recoveryTxnMtx_);
    const std::string base = orphanDir_ + "/" + id;
    std::error_code ec;
    json meta;
    {
        std::lock_guard<std::mutex> lk(orphanMtx_);
        if (!recoveryAvailable()) return false;
        if (!fs::is_regular_file(fs::u8path(base + ".step+"), ec) ||
            !validSnapshot(base + ".step+")) return false;
        bool metaReadable = false;
        meta = readMeta(base + ".json", &metaReadable);
        if (!metaReadable) { quarantine(fs::u8path(base + ".step+")); return false; }
        recoveryPins_.insert(id);
    }
    bool consumed = false;
    try {
        consumed = open(base + ".step+", meta);
    } catch (...) {
        std::lock_guard<std::mutex> lk(orphanMtx_);
        recoveryPins_.erase(id);
        if (supersededPins_.erase(id)) supersedeOrphanLocked(id);
        throw;
    }
    std::lock_guard<std::mutex> lk(orphanMtx_);
    recoveryPins_.erase(id);
    const bool superseded = supersededPins_.erase(id) != 0;
    if (!consumed && !superseded) return false;
    if (fs::remove(fs::u8path(base + ".step+"), ec) && !ec)
        fs::remove(fs::u8path(base + ".json"), ec);
    chudware_ipc::syncParentDirectory(base);
    return true;
}

void Autosave::supersedeOrphanLocked(const std::string& id) {
    if (!validId(id)) return;
    if (recoveryPins_.count(id)) {
        supersededPins_.insert(id);
        return;
    }
    std::error_code ec;
    const std::string base = orphanDir_ + "/" + id;
    if (fs::remove(fs::u8path(base + ".step+"), ec) && !ec) {
        fs::remove(fs::u8path(base + ".json"), ec);
        chudware_ipc::syncParentDirectory(base);
    }
}

int Autosave::discardOrphans(const std::string& id) {
    if (!validId(id)) return 0;
    std::lock_guard<std::mutex> transaction(recoveryTxnMtx_);
    std::lock_guard<std::mutex> lk(orphanMtx_);
    if (!recoveryAvailable()) return 0;
    std::error_code ec;
    const std::string base = orphanDir_ + "/" + id;
    const bool removed = fs::remove(fs::u8path(base + ".step+"), ec);
    if (removed && !ec) fs::remove(fs::u8path(base + ".json"), ec);
    if (removed) chudware_ipc::syncParentDirectory(base);
    return removed ? 1 : 0;
}

void Autosave::noteRecoveredRemap(const std::string& oldId, const std::string& newId) {
    if (oldId.empty() || newId.empty() || oldId == newId) return;
    std::lock_guard<std::mutex> lk(remapMtx_);
    idRemap_[oldId] = newId;
}

void Autosave::rememberRecoveredSource(const std::string& docId, const json& meta,
                                      const std::string& orphanId, long revision,
                                      long metadataRevision) {
    if (docId.empty() || !meta.is_object() || !validId(orphanId)) return;
    std::lock_guard<std::mutex> lk(revMtx_);
    recoveredSource_[docId] = RecoveredSource{meta, orphanId, revision, metadataRevision};
}

}  // namespace hostd
