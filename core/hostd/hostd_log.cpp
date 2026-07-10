#include "hostd_log.h"

#include <cstdio>
#include <ctime>

#include <sys/stat.h>

namespace hostd {

static constexpr long kMaxBytes = 5 * 1024 * 1024;
static constexpr int kGenerations = 3;

Log& log() {
    static Log l;
    return l;
}

static bool mkdirs(const std::string& dir) {
    std::string cur;
    for (size_t i = 0; i < dir.size(); ++i) {
        cur.push_back(dir[i]);
        if (dir[i] == '/' || i + 1 == dir.size()) {
            std::string node = (dir[i] == '/') ? cur.substr(0, cur.size() - 1) : cur;
            if (node.empty() || node == "/") continue;
            ::mkdir(node.c_str(), 0755);
        }
    }
    struct stat st{};
    return ::stat(dir.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
}

bool Log::open(const std::string& dir) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (!mkdirs(dir)) return false;
    path_ = dir + "/hostd.log";
    rotateIfNeeded();
    f_ = std::fopen(path_.c_str(), "a");
    return f_ != nullptr;
}

void Log::rotateIfNeeded() {
    struct stat st{};
    if (::stat(path_.c_str(), &st) != 0 || st.st_size < kMaxBytes) return;
    if (f_) {
        std::fclose(f_);
        f_ = nullptr;
    }
    for (int g = kGenerations - 1; g >= 1; --g) {
        std::string from = path_ + "." + std::to_string(g);
        std::string to = path_ + "." + std::to_string(g + 1);
        std::rename(from.c_str(), to.c_str());  // best-effort
    }
    std::rename(path_.c_str(), (path_ + ".1").c_str());
}

void Log::line(const char* fmt, ...) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (!f_) return;
    char ts[32];
    std::time_t now = std::time(nullptr);
    std::tm tm{};
    localtime_r(&now, &tm);
    std::strftime(ts, sizeof ts, "%F %T", &tm);
    std::fprintf(f_, "[%s] ", ts);
    va_list ap;
    va_start(ap, fmt);
    std::vfprintf(f_, fmt, ap);
    va_end(ap);
    std::fputc('\n', f_);
    std::fflush(f_);
    rotateIfNeeded();
}

}  // namespace hostd
