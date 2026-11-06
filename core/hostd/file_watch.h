#pragma once

#include <condition_variable>
#include <functional>
#include <mutex>
#include <string>
#include <thread>

#include <nlohmann/json.hpp>

namespace hostd {

// Polls only bound, live documents. Engine stamps distinguish external writes
// from the engine's own saves and cache writebacks.
class FileWatch {
public:
    using List = std::function<nlohmann::json()>;
    enum class Outcome { Retry, Reloaded, Conflict };
    using Changed = std::function<Outcome(const std::string&, const std::string&,
                                          const nlohmann::json&)>;

    FileWatch(List list, Changed changed);
    ~FileWatch();
    FileWatch(const FileWatch&) = delete;
    FileWatch& operator=(const FileWatch&) = delete;

    // Null when the file cannot be stated. Matches the engine's doc_list stamp.
    static nlohmann::json stamp(const std::string& path);

private:
    void run();
    List list_;
    Changed changed_;
    std::mutex mutex_;
    std::condition_variable wake_;
    bool stopping_ = false;
    std::thread thread_;
};

}  // namespace hostd
