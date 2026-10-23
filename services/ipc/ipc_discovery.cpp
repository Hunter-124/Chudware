// POSIX discovery/lifecycle. Windows private file locks and named-pipe discovery
// live in ipc_discovery_win.cpp.
#ifndef _WIN32

#include "ipc_discovery.h"
#include "ipc_private.h"

#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include <fcntl.h>
#include <limits.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>
extern char** environ;
#include <filesystem>

#include "security/integrity.h"
#include "util/platform_paths.h"

namespace chudware_ipc {

std::string fnv1aHex(const std::string& s) {
    // Rendezvous protocol constant, mirrored byte-for-byte in
    // services/mcp/daemon_bridge.py::_fnv1a64_hex. This is the repo's house FNV
    // basis (textbook value minus its last digit) — do NOT "correct" it on one
    // side only: clients and daemon would compute different socket keys and
    // split into separate engines (breaks live doc mirroring).
    uint64_t h = 1469598103934665603ull;
    for (unsigned char c : s) {
        h ^= c;
        h *= 1099511628211ull;
    }
    char buf[17];
    std::snprintf(buf, sizeof buf, "%016llx", (unsigned long long)h);
    return buf;
}

std::string instanceKey(const std::string& hostdBin) {
#ifdef CHUDWARE_DEVELOPER_BUILD
    if (const char* k = std::getenv("CHUDWARE_INSTANCE_KEY"); k && *k) return k;
#endif
    std::string id = hostdBin;
    char real[PATH_MAX];
    if (!id.empty() && ::realpath(id.c_str(), real)) id = real;
    if (id.empty()) id = "default";
    return fnv1aHex(id);
}

std::string runtimeDir() {
    // Resolution must be DETERMINISTIC across every process that can spawn or
    // discover the daemon: the flock singleton only works if the GUI, the MCP
    // bridges, and the daemon all agree on one directory. Agent-spawned
    // processes often lack XDG_RUNTIME_DIR, so an env-only rule split the
    // world into /run/user/<uid> and /tmp/chudware-<uid> — two live "singletons"
    // with the same instance key. When the env var is unset, fall back to the
    // canonical /run/user/<uid> (if it exists and is ours) BEFORE /tmp.
    // Mirrored in daemon_bridge.py socket_path().
    std::string dir;
    if (const char* x = std::getenv("XDG_RUNTIME_DIR"); x && *x) {
        dir = std::string(x) + "/chudware";
    } else {
        const std::string runUser = "/run/user/" + std::to_string((long)::getuid());
        struct stat ru{};
        if (::lstat(runUser.c_str(), &ru) == 0 && S_ISDIR(ru.st_mode) &&
            ru.st_uid == ::getuid())
            dir = runUser + "/chudware";
        else
            dir = "/tmp/chudware-" + std::to_string((long)::getuid());
    }
    if (::mkdir(dir.c_str(), 0700) != 0 && errno != EEXIST) return "";
    // Refuse a dir we don't own or that others can write (symlink squat defence).
    struct stat st{};
    if (::lstat(dir.c_str(), &st) != 0 || !S_ISDIR(st.st_mode) ||
        st.st_uid != ::getuid() || (st.st_mode & 077) != 0)
        return "";
    return dir;
}

std::string stateDir() {
    std::string base;
    if (const char* x = std::getenv("XDG_DATA_HOME"); x && *x) base = x;
    else if (const char* h = std::getenv("HOME"); h && *h) base = std::string(h) + "/.local/share";
    else base = ".";
    const std::string dir = base + "/Chudware";
    return privateDir(dir) ? dir : std::string();
}

std::string socketPath(const std::string& key) {
#ifdef CHUDWARE_DEVELOPER_BUILD
    if (const char* p = std::getenv("CHUDWARE_HOSTD_SOCKET"); p && *p) return p;
#endif
    std::string dir = runtimeDir();
    return dir.empty() ? "" : dir + "/hostd-" + key + ".sock";
}

std::string lockPath(const std::string& key) {
    std::string dir = runtimeDir();
    return dir.empty() ? "" : dir + "/hostd-" + key + ".lock";
}

std::string guiLockPath(const std::string& key) {
    // Deliberately NOT named "hostd-*": the daemon's stale-endpoint sweep (core/hostd/main.cpp)
    // only reaps hostd-*.lock pairs, and this file must survive it — as with every lock here the
    // flock, not the file's existence, is the claim.
    std::string dir = runtimeDir();
    return dir.empty() ? "" : dir + "/gui-" + key + ".lock";
}

std::string resolveHostdBin(const std::string& hint) {
#ifdef CHUDWARE_DEVELOPER_BUILD
    if (const char* b = std::getenv("CHUDWARE_HOSTD_BIN"); b && *b) return b;
    if (!hint.empty() && ::access(hint.c_str(), X_OK) == 0) return hint;
    return "";
#else
    (void)hint;
    namespace fs = std::filesystem;
    const fs::path self = chudware::platform::executablePath();
    if (self.empty()) return "";
    const fs::path root = chudware::security::installedPayloadRoot(self);
    const fs::path candidate = self.parent_path() / "chudware-hostd";
    std::error_code ec;
    const std::string relative = fs::relative(candidate, root, ec).generic_string();
    if (ec || relative.empty() || relative.rfind("../", 0) == 0) return "";
    const auto verified =
        chudware::security::verifyInstalledManifestEntry(self, relative, "worker");
    return verified.ok ? candidate.string() : std::string();
#endif
}

bool SingleInstance::acquire(const std::string& lockFile, std::string& err) {
    release();
    int fd = ::open(lockFile.c_str(), O_RDWR | O_CREAT | O_CLOEXEC, 0600);
    if (fd < 0) {
        err = "open " + lockFile + ": " + std::strerror(errno);
        return false;
    }
    if (::flock(fd, LOCK_EX | LOCK_NB) != 0) {
        err = "another instance holds " + lockFile;
        ::close(fd);
        return false;
    }
    // PID recorded for diagnostics only — liveness comes from the flock itself.
    if (::ftruncate(fd, 0) == 0) {
        std::string pid = std::to_string((long)::getpid()) + "\n";
        ssize_t rc = ::write(fd, pid.c_str(), pid.size());
        (void)rc;
    }
    fd_ = fd;
    path_ = lockFile;
    return true;
}

void SingleInstance::release() {
    // Order (drain-window): drop the flock FIRST, then unlink the sock.
    // Clients that see lock-held-no-sock during drain previously burned their full
    // connect budget; now the sock stays until we release, and spawn-losers that
    // wait on the flock wake as soon as we exit.
    if (fd_ >= 0) {
        ::close(fd_);  // drops the flock
        fd_ = -1;
        path_.clear();
    }
    if (!sock_path_.empty()) {
        ::unlink(sock_path_.c_str());
        sock_path_.clear();
    }
}

bool spawnDetached(const std::string& exe, const std::string& arg, std::string& err) {
#ifndef CHUDWARE_DEVELOPER_BUILD
    namespace fs = std::filesystem;
    const fs::path self = chudware::platform::executablePath();
    const fs::path root = chudware::security::installedPayloadRoot(self);
    std::error_code ec;
    const std::string relative = fs::relative(fs::path(exe), root, ec).generic_string();
    if (ec || relative.empty() || relative.rfind("../", 0) == 0) {
        err = "hostd path is outside the installed payload";
        return false;
    }
    const int pinnedFd = ::open(exe.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (pinnedFd < 0) {
        err = std::string("open verified hostd: ") + std::strerror(errno);
        return false;
    }
    const auto verified =
        chudware::security::verifyInstalledManifestFd(self, relative, "worker", pinnedFd);
    if (!verified.ok) {
        ::close(pinnedFd);
        err = "hostd integrity verification failed: " + verified.detail;
        return false;
    }
#endif
    pid_t pid = ::fork();
    if (pid < 0) {
#ifndef CHUDWARE_DEVELOPER_BUILD
        ::close(pinnedFd);
#endif
        err = std::string("fork: ") + std::strerror(errno);
        return false;
    }
    if (pid == 0) {
        ::setsid();
        pid_t pid2 = ::fork();
        if (pid2 != 0) ::_exit(0);
        int devnull = ::open("/dev/null", O_RDWR);
        if (devnull >= 0) {
            ::dup2(devnull, 0); ::dup2(devnull, 1); ::dup2(devnull, 2);
            if (devnull > 2) ::close(devnull);
        }
#ifdef CHUDWARE_DEVELOPER_BUILD
        if (arg.empty()) ::execl(exe.c_str(), exe.c_str(), (char*)nullptr);
        else ::execl(exe.c_str(), exe.c_str(), arg.c_str(), (char*)nullptr);
#else
        char* const argvNoArg[] = {const_cast<char*>(exe.c_str()), nullptr};
        char* const argvArg[] = {const_cast<char*>(exe.c_str()), const_cast<char*>(arg.c_str()), nullptr};
#ifdef __APPLE__
        // macOS has no fexecve. F_GETPATH re-derives the verified binary's path
        // from the pinned fd (the fd itself was integrity-checked before this
        // point, and the path is what execve needs), which is the accepted
        // Darwin equivalent.
        char resolved[PATH_MAX];
        if (::fcntl(pinnedFd, F_GETPATH, resolved) == 0)
            ::execve(resolved, arg.empty() ? argvNoArg : argvArg, environ);
#else
        ::fexecve(pinnedFd, arg.empty() ? argvNoArg : argvArg, environ);
#endif
#endif
        ::_exit(127);
    }
#ifndef CHUDWARE_DEVELOPER_BUILD
    ::close(pinnedFd);
#endif
    int status = 0;
    ::waitpid(pid, &status, 0);
    return true;
}

}  // namespace chudware_ipc

#endif  // !_WIN32
