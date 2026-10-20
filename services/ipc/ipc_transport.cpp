// POSIX (AF_UNIX) implementation of the hostd IPC transport. Windows named-pipe
// backend lands with the Windows-parity phase; until then this file is compiled
// on POSIX only (guarded in CMake, plus the #ifndef below as a belt).
#ifndef _WIN32

#include "ipc_transport.h"

#include <algorithm>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <chrono>

#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace chudware_ipc {
// SOCK_CLOEXEC and accept4() are Linux extensions; Darwin has neither. Descriptors
// must still be close-on-exec — hostd forks agent workers, the python worker and
// the omp bridge, and a leaked listen fd there keeps the socket bound after the
// daemon exits, so the next daemon's stale-socket probe finds a live peer and
// refuses to start. The two-step fallback has a fork window; hostd's own spawn
// paths close everything from fd 3 up before exec (chudware::platform::
// closeInheritedFds), which covers it.
namespace {

int setCloexec(int fd) {
    if (fd < 0) return fd;
    const int fl = ::fcntl(fd, F_GETFD);
    if (fl >= 0) ::fcntl(fd, F_SETFD, fl | FD_CLOEXEC);
    return fd;
}

int cloexecSocket() {
#if defined(SOCK_CLOEXEC)
    return ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
#else
    return setCloexec(::socket(AF_UNIX, SOCK_STREAM, 0));
#endif
}

int cloexecAccept(int listenFd) {
#if defined(__linux__)
    return ::accept4(listenFd, nullptr, nullptr, SOCK_CLOEXEC);
#else
    return setCloexec(::accept(listenFd, nullptr, nullptr));
#endif
}

}  // namespace

IpcListener& IpcListener::operator=(IpcListener&& o) noexcept {
    if (this != &o) {
        close();
        fd_ = o.fd_;
        path_ = std::move(o.path_);
        o.fd_ = -1;
        o.path_.clear();
    }
    return *this;
}

IpcConn& IpcConn::operator=(IpcConn&& o) noexcept {
    if (this != &o) {
        close();
        fd_ = o.fd_;
        eof_.store(o.eof_.load(std::memory_order_relaxed), std::memory_order_relaxed);
        readBuf_ = std::move(o.readBuf_);
        o.fd_ = -1;
        o.eof_.store(false, std::memory_order_relaxed);
        o.readBuf_.clear();
    }
    return *this;
}

void IpcConn::close() {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
}

void IpcConn::shutdown() {
    // Half-close so a peer thread parked in readLine()'s poll() wakes immediately
    // (POLLHUP). Deliberately does NOT touch fd_ — the owning thread still closes it
    // — so this is safe to call cross-thread from the daemon shutdown path. shutdown()
    // on an already-closed/errored fd is a harmless EBADF/ENOTCONN.
    if (fd_ >= 0) ::shutdown(fd_, SHUT_RDWR);
}

bool IpcConn::readLine(std::string& line, int timeoutMs) {
    if (fd_ < 0) return false;
    constexpr size_t kMaxLine = 64u * 1024u * 1024u;
    const auto start = std::chrono::steady_clock::now();
    size_t scanFrom = 0;
    for (;;) {
        size_t nl = readBuf_.find('\n', scanFrom);
        if ((nl != std::string::npos && nl > kMaxLine) ||
            (nl == std::string::npos && readBuf_.size() > kMaxLine)) {
            readBuf_.clear();
            eof_.store(true, std::memory_order_release);
            return false;
        }
        scanFrom = readBuf_.size();
        if (nl != std::string::npos) {
            line = readBuf_.substr(0, nl);
            readBuf_.erase(0, nl + 1);
            return true;
        }
        if (eof_) return false;

        int wait = -1;
        if (timeoutMs >= 0) {
            auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                               std::chrono::steady_clock::now() - start)
                               .count();
            wait = timeoutMs - (int)elapsed;
            if (wait < 0) return false;
        }
        struct pollfd pfd{fd_, POLLIN | POLLHUP | POLLERR, 0};
        int pr = ::poll(&pfd, 1, wait);
        if (pr < 0) {
            if (errno == EINTR) continue;
            eof_.store(true, std::memory_order_release);
            return false;
        }
        if (pr == 0) return false;  // timeout (incl. the 0 ms non-blocking probe)

        char buf[16384];
        ssize_t got = ::read(fd_, buf, sizeof buf);
        if (got > 0) {
            readBuf_.append(buf, (size_t)got);
        } else if (got == 0 || (errno != EINTR && errno != EAGAIN)) {
            eof_.store(true, std::memory_order_release);  // peer closed (partial line dropped)
            if (got == 0 && readBuf_.find('\n') == std::string::npos) return false;
        }
    }
}

bool IpcConn::writeLine(const std::string& line) {
    if (fd_ < 0) return false;
    std::string out = line;
    out.push_back('\n');
    size_t off = 0;
    while (off < out.size()) {
        ssize_t n = ::write(fd_, out.data() + off, out.size() - off);
        if (n > 0) {
            off += (size_t)n;
        } else if (errno == EINTR) {
            continue;
        } else {
            // A slow/wedged peer whose recv buffer is full makes write() block;
            // accepted daemon sockets carry SO_SNDTIMEO so that surfaces here as
            // EAGAIN/EWOULDBLOCK instead of an unbounded hang. Either way the
            // stream is now desynced (a partial line may have gone out), so the
            // connection is dead: flag EOF (the owning client loop tears it down)
            // and fail. This is the backstop that stops one stuck GUI from
            // stalling event fanout to every other client — see Server fanout.
            eof_.store(true, std::memory_order_release);
            return false;
        }
    }
    return true;
}

static bool mkParentDirs(const std::string& path) {
    // Create every missing parent with 0700 (runtime dirs hold user-private sockets).
    std::string dir = path.substr(0, path.find_last_of('/'));
    if (dir.empty()) return true;
    std::string cur;
    for (size_t i = 0; i < dir.size(); ++i) {
        cur.push_back(dir[i]);
        if (dir[i] == '/' || i + 1 == dir.size()) {
            if (cur == "/") continue;
            std::string node = (dir[i] == '/') ? cur.substr(0, cur.size() - 1) : cur;
            if (node.empty()) continue;
            if (::mkdir(node.c_str(), 0700) != 0 && errno != EEXIST) return false;
        }
    }
    return true;
}

bool IpcListener::listen(const std::string& path, std::string& err) {
    close();
    if (path.size() >= sizeof(sockaddr_un{}.sun_path)) {
        err = "socket path too long: " + path;
        return false;
    }
    if (!mkParentDirs(path)) {
        err = "mkdir failed for " + path + ": " + std::strerror(errno);
        return false;
    }
    // Stale-socket handling: probe-connect. Caller holds the instance lock, so a
    // refused/absent peer means the file is a leftover from a dead daemon.
    {
        std::string probeErr;
        IpcConn probe = connectTo(path, probeErr);
        if (probe.valid()) {
            err = "another daemon is already listening on " + path;
            return false;
        }
        ::unlink(path.c_str());
    }

    fd_ = cloexecSocket();
    if (fd_ < 0) {
        err = std::string("socket: ") + std::strerror(errno);
        return false;
    }
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    std::strncpy(addr.sun_path, path.c_str(), sizeof(addr.sun_path) - 1);
    if (::bind(fd_, (sockaddr*)&addr, sizeof addr) != 0) {
        err = "bind " + path + ": " + std::strerror(errno);
        close();
        return false;
    }
    ::chmod(path.c_str(), 0600);  // same-user only (dir is 0700 too)
    if (::listen(fd_, 16) != 0) {
        err = std::string("listen: ") + std::strerror(errno);
        close();
        return false;
    }
    path_ = path;
    return true;
}

bool IpcListener::adoptListeningFd(int fd, std::string& err) {
    close();
    if (fd < 0) {
        err = "invalid inherited listener fd";
        return false;
    }
    fd_ = fd;  // close() owns cleanup on every failure; path_ stays empty.
    sockaddr_storage address{};
    socklen_t length = sizeof address;
    int type = 0, accepting = 0;
    socklen_t optionLength = sizeof type;
    if (::getsockname(fd_, reinterpret_cast<sockaddr*>(&address), &length) != 0 ||
        address.ss_family != AF_UNIX ||
        ::getsockopt(fd_, SOL_SOCKET, SO_TYPE, &type, &optionLength) != 0 ||
        type != SOCK_STREAM) {
        err = "inherited listener must be an AF_UNIX stream socket";
        close();
        return false;
    }
    optionLength = sizeof accepting;
    if (::getsockopt(fd_, SOL_SOCKET, SO_ACCEPTCONN, &accepting, &optionLength) != 0 ||
        !accepting) {
        err = "inherited socket is not listening";
        close();
        return false;
    }
    const int flags = ::fcntl(fd_, F_GETFD);
    if (flags < 0 || ::fcntl(fd_, F_SETFD, flags | FD_CLOEXEC) != 0) {
        err = std::string("inherited listener CLOEXEC: ") + std::strerror(errno);
        close();
        return false;
    }
    err.clear();
    return true;
}

IpcConn IpcListener::accept(int timeoutMs) {
    if (fd_ < 0) return IpcConn();
    struct pollfd pfd{fd_, POLLIN, 0};
    int pr = ::poll(&pfd, 1, timeoutMs);
    if (pr <= 0) return IpcConn();
    int c = cloexecAccept(fd_);
    if (c < 0) return IpcConn();
    // Bound every write to this client. Without it, a client whose recv buffer
    // fills (a busy/wedged GUI under a heavy multi-agent event stream) makes the
    // daemon's write() block forever — and event fanout runs on the acting
    // client's dispatch thread and on the accept loop (tick flush), so ONE stuck
    // consumer could stall the whole daemon. On timeout the write fails
    // (EAGAIN) and the connection is dropped. Configurable; 0 disables.
    static const int sndTimeoutS = [] {
        if (const char* s = std::getenv("CHUDWARE_HOSTD_WRITE_TIMEOUT_S"); s && *s)
            return std::max(0, std::atoi(s));
        return 10;  // headroom for a merely-busy GUI; a dropped client reconnects
    }();
    if (sndTimeoutS > 0) {
        struct timeval tv{sndTimeoutS, 0};
        ::setsockopt(c, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    }
    return IpcConn(c);
}

std::string IpcListener::releasePath() {
    std::string p = std::move(path_);
    path_.clear();
    return p;
}

void IpcListener::close(bool unlink_path) {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
    }
    // T-PERF10: unlink is optional so hostd can drop the listen fd while the
    // single-instance flock still covers the sock file, then unlink at flock
    // release (no lock-held-but-no-sock window for spawn-on-demand clients).
    if (unlink_path && !path_.empty()) ::unlink(path_.c_str());
    path_.clear();
}

IpcConn connectTo(const std::string& path, std::string& err) {
    int fd = cloexecSocket();
    if (fd < 0) {
        err = std::string("socket: ") + std::strerror(errno);
        return IpcConn();
    }
    sockaddr_un addr{};
    addr.sun_family = AF_UNIX;
    std::strncpy(addr.sun_path, path.c_str(), sizeof(addr.sun_path) - 1);
    if (::connect(fd, (sockaddr*)&addr, sizeof addr) != 0) {
        err = "connect " + path + ": " + std::strerror(errno);
        ::close(fd);
        return IpcConn();
    }
    return IpcConn(fd);
}

}  // namespace chudware_ipc

#endif  // !_WIN32
