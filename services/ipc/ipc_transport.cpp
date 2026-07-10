// POSIX (AF_UNIX) implementation of the hostd IPC transport. Windows named-pipe
// backend lands with the Windows-parity phase; until then this file is compiled
// on POSIX only (guarded in CMake, plus the #ifndef below as a belt).
#ifndef _WIN32

#include "ipc_transport.h"

#include <cerrno>
#include <cstring>
#include <chrono>

#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace aicad_ipc {

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
        eof_ = o.eof_;
        readBuf_ = std::move(o.readBuf_);
        o.fd_ = -1;
        o.eof_ = false;
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

bool IpcConn::readLine(std::string& line, int timeoutMs) {
    if (fd_ < 0) return false;
    const auto start = std::chrono::steady_clock::now();
    for (;;) {
        size_t nl = readBuf_.find('\n');
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
        struct pollfd pfd{fd_, POLLIN, 0};
        int pr = ::poll(&pfd, 1, wait);
        if (pr < 0) {
            if (errno == EINTR) continue;
            eof_ = true;
            return false;
        }
        if (pr == 0) return false;  // timeout (incl. the 0 ms non-blocking probe)

        char buf[16384];
        ssize_t got = ::read(fd_, buf, sizeof buf);
        if (got > 0) {
            readBuf_.append(buf, (size_t)got);
        } else if (got == 0 || (errno != EINTR && errno != EAGAIN)) {
            eof_ = true;  // peer closed (a buffered partial line without \n is dropped)
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
            eof_ = true;
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

    fd_ = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
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

IpcConn IpcListener::accept(int timeoutMs) {
    if (fd_ < 0) return IpcConn();
    struct pollfd pfd{fd_, POLLIN, 0};
    int pr = ::poll(&pfd, 1, timeoutMs);
    if (pr <= 0) return IpcConn();
    int c = ::accept4(fd_, nullptr, nullptr, SOCK_CLOEXEC);
    return c >= 0 ? IpcConn(c) : IpcConn();
}

void IpcListener::close() {
    if (fd_ >= 0) {
        ::close(fd_);
        fd_ = -1;
        if (!path_.empty()) ::unlink(path_.c_str());
        path_.clear();
    }
}

IpcConn connectTo(const std::string& path, std::string& err) {
    int fd = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
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

}  // namespace aicad_ipc

#endif  // !_WIN32
