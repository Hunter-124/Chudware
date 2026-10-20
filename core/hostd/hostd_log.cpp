#include "hostd_log.h"

#include <cstdint>
#include <cstdio>
#include <ctime>
#include <filesystem>
#include <system_error>

#include "util/time_local.h"
#include "../../services/ipc/ipc_private.h"

namespace hostd {

static constexpr long kMaxBytes = 5 * 1024 * 1024;
static constexpr int kGenerations = 3;

Log& log() {
    static Log l;
    return l;
}


bool Log::open(const std::string& dir) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (!chudware_ipc::privateDir(dir)) return false;
    path_ = dir + "/hostd.log";
    rotateIfNeeded();
    f_ = chudware_ipc::privateFile(path_, true);
    return f_ != nullptr;
}

void Log::rotateIfNeeded() {
    std::error_code ec;
    const auto size = std::filesystem::file_size(path_, ec);
    if (ec || size < (std::uintmax_t)kMaxBytes) return;
    const bool wasOpen = f_ != nullptr;
    if (f_) {
        std::fclose(f_);  // Windows requires the handle closed before rename
        f_ = nullptr;
    }
    for (int g = kGenerations - 1; g >= 1; --g) {
        std::string from = path_ + "." + std::to_string(g);
        std::string to = path_ + "." + std::to_string(g + 1);
        std::filesystem::rename(from, to, ec);  // best-effort (replaces `to` on both platforms)
        (void)ec;
    }
    std::filesystem::rename(path_, path_ + ".1", ec);
    if (wasOpen) f_ = chudware_ipc::privateFile(path_, true);
}

void Log::line(const char* fmt, ...) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (!f_) return;
    char ts[32];
    std::time_t now = std::time(nullptr);
    std::tm tm{};
    (void)chudware::util::localTime(now, tm);
    // Spelled out (not %F %T): pre-UCRT Windows CRTs lack the C99 specifiers.
    std::strftime(ts, sizeof ts, "%Y-%m-%d %H:%M:%S", &tm);
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
