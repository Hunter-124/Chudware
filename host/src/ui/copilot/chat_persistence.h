#pragma once

// Private conversation persistence: one active immutable snapshot and one coalesced pending
// snapshot. The UI never takes a lock held across storage calls. Explicit barriers wait for
// one attempt, not forever on an unavailable filesystem; failed snapshots remain pending.
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>

namespace ui::chat_persistence {
using Bytes = std::shared_ptr<const std::string>;
struct Snapshot {
    std::map<std::string, Bytes> files;
    std::string indexPath;
    std::string directory;
    std::set<std::string> keep;
    std::set<std::string> owned;
    bool history = false;
};

class Writer {
public:
    Writer() : thread_([this] { run(); }) {}
    ~Writer() {
        flush();
        { std::lock_guard<std::mutex> lock(mutex_); stopping_ = true; }
        wake_.notify_one();
        thread_.join();
    }
    void submit(Snapshot snapshot) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (snapshot.history) {
                // Drop superseded transcript writes, including sessions deleted while a save
                // was in flight. Preserve the independently coalesced composer draft.
                for (auto it = pending_.files.begin(); it != pending_.files.end();) {
                    if (it->first != draftPath_) it = pending_.files.erase(it);
                    else ++it;
                }
                pending_.history = true;
                pending_.directory = std::move(snapshot.directory);
                pending_.indexPath = std::move(snapshot.indexPath);
                pending_.keep = std::move(snapshot.keep);
                pending_.owned.insert(snapshot.owned.begin(), snapshot.owned.end());
            } else if (!snapshot.files.empty()) {
                draftPath_ = snapshot.files.begin()->first;
            }
            for (auto& [path, bytes] : snapshot.files) pending_.files[path] = std::move(bytes);
            ++requested_;
            retry_ = false;
        }
        wake_.notify_one();
    }
    bool flush() {
        std::unique_lock<std::mutex> lock(mutex_);
        if (pending_.files.empty() && !active_) return error_.empty();
        const auto target = ++requested_;
        retry_ = false;
        wake_.notify_one();
        done_.wait(lock, [&] { return attempted_ >= target; });
        return error_.empty();
    }
    std::string takeError() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (reported_ == error_) return {};
        reported_ = error_;
        return error_;
    }
    void forgetDurable() {
        // Caller has completed a successful read-after-write barrier before reloading.
        std::lock_guard<std::mutex> lock(mutex_);
        durable_.clear();
    }
private:
    bool write(const Snapshot& snapshot, std::string& error) {
        namespace fs = std::filesystem;
        owned_.insert(snapshot.owned.begin(), snapshot.owned.end());
        auto save = [&](const std::string& path, const Bytes& bytes) {
            auto old = durable_.find(path);
            if (old == durable_.end() && !needsDurable_.count(path)) {
                // Seed from existing files off-thread. Startup/reload of unchanged history
                // must not generate a round of fsyncs just to establish the cache.
                std::ifstream in(path, std::ios::binary);
                if (in) {
                    std::ostringstream data;
                    data << in.rdbuf();
                    if (!in.bad()) {
                        durable_[path] = std::make_shared<const std::string>(data.str());
                        old = durable_.find(path);
                    }
                }
            }
            if (old != durable_.end() && (old->second == bytes || *old->second == *bytes)) {
                old->second = bytes;
                if (snapshot.history && snapshot.keep.count(fs::path(path).stem().string()))
                    owned_.insert(fs::path(path).stem().string());
                return true;
            }
            std::error_code ec;
            fs::create_directories(fs::path(path).parent_path(), ec);
            bool replaced = false;
            const bool ok = !ec && atomicWriteFileDurableImpl(path, *bytes, &replaced);
            if (replaced && snapshot.history &&
                snapshot.keep.count(fs::path(path).stem().string()))
                owned_.insert(fs::path(path).stem().string());
            if (!ok) {
                if (replaced) {
                    durable_.erase(path);
                    needsDurable_.insert(path);
                }
                error = "[chat] durable save failed: " + path + " (pending state retained; retrying)";
                return false;
            }
            durable_[path] = bytes;
            needsDurable_.erase(path);
            if (snapshot.history && path != snapshot.indexPath &&
                snapshot.keep.count(fs::path(path).stem().string()))
                owned_.insert(fs::path(path).stem().string());
            return true;
        };
        // Never publish an index or prune until every referenced transcript is durable.
        for (const auto& [path, bytes] : snapshot.files)
            if (path != snapshot.indexPath && !save(path, bytes)) return false;
        const auto index = snapshot.files.find(snapshot.indexPath);
        if (index != snapshot.files.end() && !save(index->first, index->second)) return false;
        if (!snapshot.history) return true;
        for (auto it = owned_.begin(); it != owned_.end();) {
            if (snapshot.keep.count(*it)) { ++it; continue; }
            const std::string base = snapshot.directory + "/" + *it;
            std::error_code ec;
            if (fs::remove(base + ".json", ec)) pruneNeedsSync_.insert(*it);
            if (ec) { error = "[chat] session removal failed: " + base; return false; }
            if (fs::remove_all(base + ".subagents", ec)) pruneNeedsSync_.insert(*it);
            ec.clear();
            if (fs::remove(base + ".raw.jsonl", ec)) pruneNeedsSync_.insert(*it);
            durable_.erase(base + ".json");
            if (pruneNeedsSync_.count(*it) && !fsyncPath(snapshot.directory, true)) {
                error = "[chat] session removal flush failed: " + snapshot.directory;
                return false;
            }
            pruneNeedsSync_.erase(*it);
            needsDurable_.erase(base + ".json");
            it = owned_.erase(it);
        }
        return true;
    }
    void run() {
        std::unique_lock<std::mutex> lock(mutex_);
        for (;;) {
            if (retry_) wake_.wait_for(lock, std::chrono::seconds(1), [&] { return stopping_ || !retry_; });
            else wake_.wait(lock, [&] { return stopping_ || requested_ > attempted_; });
            if (stopping_) return;
            Snapshot snapshot = pending_;
            const auto generation = requested_;
            active_ = true;
            lock.unlock();
            std::string error;
            bool ok = false;
            try { ok = write(snapshot, error); }
            catch (const std::exception& e) { error = std::string("[chat] durable save failed: ") + e.what(); }
            catch (...) { error = "[chat] durable save failed (pending state retained)"; }
            lock.lock();
            if (!ok && error != error_) std::fprintf(stderr, "%s\n", error.c_str());
            error_ = std::move(error);
            active_ = false;
            attempted_ = generation;
            if (ok && requested_ == generation) pending_ = Snapshot{};
            retry_ = !ok && requested_ == generation;
            done_.notify_all();
        }
    }
    std::mutex mutex_;
    std::condition_variable wake_, done_;
    Snapshot pending_;
    std::map<std::string, Bytes> durable_;
    std::set<std::string> owned_, pruneNeedsSync_, needsDurable_;
    std::string draftPath_, error_, reported_;
    unsigned long long requested_ = 0, attempted_ = 0;
    bool active_ = false, retry_ = false, stopping_ = false;
    std::thread thread_;
};
}  // namespace ui::chat_persistence
