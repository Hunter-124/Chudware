// Per-document mesh obligations. A fetched path is not complete until its mesh commits.
#pragma once

#include <algorithm>
#include <chrono>
#include <map>
#include <string>

namespace core {

class TessCatchup {
public:
    using Clock = std::chrono::steady_clock;

    void require(const std::string& docId, long revision) {
        if (docId.empty() || revision < 0) return;
        State& state = docs_[docId];
        if (revision > state.wanted) {
            state.wanted = revision;
            state.loading = -1;
            state.retryAt = {};
            state.failures = 0;
        }
    }

    bool beginFetch(const std::string& docId, Clock::time_point now = Clock::now()) {
        auto it = docs_.find(docId);
        if (it == docs_.end() || it->second.inFlight ||
            it->second.loading >= it->second.wanted || now < it->second.retryAt)
            return false;
        it->second.inFlight = true;
        return true;
    }

    void fetched(const std::string& docId, bool usable, Clock::time_point now = Clock::now()) {
        auto it = docs_.find(docId);
        if (it == docs_.end()) return;
        it->second.inFlight = false;
        if (!usable) retry(it->second, now);
    }

    void loading(const std::string& docId, long revision) {
        auto it = docs_.find(docId);
        if (it != docs_.end() && revision >= it->second.wanted)
            it->second.loading = revision;
    }

    void finished(const std::string& docId, long revision, bool success,
                  Clock::time_point now = Clock::now()) {
        auto it = docs_.find(docId);
        if (it == docs_.end()) return;
        if (success && revision >= it->second.wanted) {
            docs_.erase(it);
            return;
        }
        if (it->second.loading == revision) {
            it->second.loading = -1;
            if (!success) retry(it->second, now);
        }
    }

    void forget(const std::string& docId) { docs_.erase(docId); }
    bool pending(const std::string& docId) const { return docs_.count(docId) != 0; }
    bool empty() const { return docs_.empty(); }
    long wanted(const std::string& docId) const {
        auto it = docs_.find(docId);
        return it == docs_.end() ? -1 : it->second.wanted;
    }

private:
    struct State {
        long wanted = -1;
        long loading = -1;
        bool inFlight = false;
        unsigned failures = 0;
        Clock::time_point retryAt{};
    };
    static void retry(State& state, Clock::time_point now) {
        const unsigned shift = std::min(state.failures++, 5u);
        state.retryAt = now + std::chrono::milliseconds(250u << shift);
    }
    std::map<std::string, State> docs_;
};

}  // namespace core
