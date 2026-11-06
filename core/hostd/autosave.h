// Daemon-side durable snapshots and recovery of non-discarded work.
#pragma once

#include <atomic>
#include <condition_variable>
#include <functional>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <utility>

#include <nlohmann/json.hpp>

namespace hostd {

class Autosave {
public:
    bool start(const std::string& baseDir, const std::atomic<int>* engineState);
    void stop(bool keepForRecovery);

    nlohmann::json idRemap();
    bool recoveryDone() const { return recoveryDone_.load(); }
    void waitForRecovery();
    bool recoveryAvailable() const;

    // Recovery entries contain the original bound path, never the private artifact path.
    nlohmann::json recoveryList(const std::string& scope = {});
    int orphanCount();
    // Pin the selected artifact through open; consume it only after a
    // replacement snapshot, save, or retirement has durably covered it.
    bool recoverOrphan(const std::string& id,
                       const std::function<bool(const std::string&, const nlohmann::json&)>& open);
    int discardOrphans(const std::string& id);
    void noteRecoveredRemap(const std::string& oldId, const std::string& newId);
    void rememberRecoveredSource(const std::string& docId, const nlohmann::json& meta,
                                 const std::string& orphanId, long revision, long metadataRevision);
    bool snapshotNow(const std::string& docId);
    void onDocClosed(const std::string& docId);
    void onDocSaved(const std::string& docId, long savedRevision, long metadataRevision);

    // Persist a captured revision without dropping the session snapshot. The
    // caller checks the returned revisions under the registry lock before close.
    bool retireDoc(const std::string& docId, const std::string& name,
                   long& revision, long& metadataRevision);

    bool isAutosavePath(const std::string& path) const {
        return !baseDir_.empty() && path.rfind(baseDir_, 0) == 0;
    }
    void setInternalFilter(std::function<bool(const std::string&)> fn) {
        isInternal_ = std::move(fn);
    }
    void setRegistryAnnotator(std::function<void(nlohmann::json&)> fn) {
        annotate_ = std::move(fn);
    }
    void setCaptureGuard(
        std::function<void(const std::string&, const std::function<void()>&)> fn) {
        captureGuard_ = std::move(fn);
    }
    bool isInternalDoc(const std::string& docId) const {
        return isInternal_ && isInternal_(docId);
    }

private:
    void loop();
    void recoverPriorSession();
    bool migrateSession(const std::string& dir);
    void autoRestoreOrphans();
    bool saveDirtyDocs(bool force = false);
    bool docRow(const std::string& id, nlohmann::json& row);
    bool capture(const nlohmann::json& row, const std::string& directory,
                 long& revision, long& metadataRevision, std::string& published);
    bool captureGuarded(const nlohmann::json& row, const std::string& directory,
                        long& revision, long& metadataRevision, std::string& published);
    bool snapshotDoc(const nlohmann::json& row);
    void discardSessionSnapshot(const std::string& id);
    void supersedeOrphanLocked(const std::string& id);
    void snapshotFailure(const std::string& id, long revision, long metadataRevision,
                         const std::string& message);

    std::string baseDir_;
    std::string sessionDir_;
    std::string orphanDir_;
    const std::atomic<int>* engineState_ = nullptr;
    double intervalS_ = 60.0;
    std::function<bool(const std::string&)> isInternal_;
    std::function<void(nlohmann::json&)> annotate_;
    std::function<void(const std::string&, const std::function<void()>&)> captureGuard_;

    std::thread thread_;
    std::mutex mtx_;
    std::condition_variable cv_;
    std::atomic<bool> stopping_{false};
    std::atomic<bool> recoveryDone_{false};
    std::atomic<bool> recoveryIncomplete_{false};
    std::atomic<bool> recoveryFailureLogged_{false};
    std::atomic<bool> scanFailureLogged_{false};
    std::mutex migrationMtx_;

    std::mutex remapMtx_;
    nlohmann::json idRemap_ = nlohmann::json::object();
    // Protected together so saves/close invalidate in-flight snapshot publication.
    std::mutex revMtx_;
    std::map<std::string, std::pair<long, long>> lastSavedRev_;
    std::map<std::string, std::string> lastSavedPath_;
    std::map<std::string, std::pair<long, long>> savedCutoff_;
    std::map<std::string, std::pair<long, long>> failedRev_;
    std::map<std::string, unsigned long long> snapshotEpoch_;
    struct RecoveredSource {
        nlohmann::json meta;
        std::string orphanId;
        long revision = -1;
        long metadataRevision = -1;
    };
    std::map<std::string, RecoveredSource> recoveredSource_;
    std::atomic<unsigned long long> snapshotSerial_{0};
    std::mutex recoveryTxnMtx_;  // serializes list/open/discard with migration
    std::mutex orphanMtx_;
    std::set<std::string> recoveryPins_;
    std::set<std::string> supersededPins_;
};

}  // namespace hostd
