// Daemon discovery + lifecycle primitives shared by aicad-hostd and every client
// (GUI DaemonConn, Python daemon_bridge reimplements the same contract).
//
// Instance identity: every socket/lock name embeds an "instance key" so a dev
// tree and an installed app (or two installed versions) never share a daemon:
//   1. $AICAD_INSTANCE_KEY (explicit override — tests, AppImage AppRun which
//      derives it from $APPIMAGE because squashfs mounts are randomized), else
//   2. hash of the canonical (realpath) hostd binary path.
// The key is 16 hex chars of FNV-1a 64 (stable across builds/platforms; this is
// a namespacing key, not a security boundary — the 0700 runtime dir is that).
//
// Endpoints (Linux): $XDG_RUNTIME_DIR/aicad/hostd-<key>.sock, falling back to
// /tmp/aicad-<uid>/hostd-<key>.sock (dir created 0700, ownership verified).
// $AICAD_HOSTD_SOCKET overrides the full path.
#pragma once

#include <string>

namespace aicad_ipc {

// FNV-1a 64 of `s`, as 16 lowercase hex chars.
std::string fnv1aHex(const std::string& s);

// The instance key for a given hostd binary path ("" = resolve via env only).
std::string instanceKey(const std::string& hostdBin);

// Socket + lock paths for `key`. Both live in the per-user runtime dir.
std::string socketPath(const std::string& key);
std::string lockPath(const std::string& key);

// Runtime dir ($XDG_RUNTIME_DIR/aicad or /tmp/aicad-<uid>), created 0700 with
// ownership verified (refuses a dir owned by someone else). "" on failure.
std::string runtimeDir();

// State dir for logs/autosaves: ~/.local/share/AI-CAD (or $XDG_DATA_HOME/AI-CAD).
std::string stateDir();

// Resolve the hostd binary to spawn: $AICAD_HOSTD_BIN, else `hint` (a path the
// caller knows, e.g. next to its own binary), else "" (caller reports failure).
std::string resolveHostdBin(const std::string& hint);

// Single-instance guard: flock on POSIX, named mutex on Windows (lockPath()
// returns a file path / a Global\ mutex name respectively). Held for the
// daemon's lifetime; evaporates on process death, so there is never a stale
// lock to clean up.
class SingleInstance {
public:
    ~SingleInstance() { release(); }
    // Try to take the lock (non-blocking). False = another daemon holds it.
    bool acquire(const std::string& lockFile, std::string& err);
    void release();
    // T-PERF10: sock path to unlink when the flock is released (so the sock
    // file never disappears while the lock is still held). Empty = no-op.
    void setSockPathToUnlink(const std::string& sockPath) { sock_path_ = sockPath; }
#ifdef _WIN32
    bool held() const { return h_ != nullptr; }
#else
    bool held() const { return fd_ >= 0; }
#endif

private:
#ifdef _WIN32
    void* h_ = nullptr;  // mutex HANDLE
#else
    int fd_ = -1;
#endif
    std::string path_;
    std::string sock_path_;  // deferred unlink (T-PERF10 drain-window)
};

// Spawn `argv` fully detached (double-fork + setsid, fds to /dev/null) so the
// daemon outlives the spawning client. Returns false + err on failure. This is
// the replacement for the weak std::system() spawn in host/src/bootstrap.cpp.
bool spawnDetached(const std::string& exe, const std::string& arg, std::string& err);

}  // namespace aicad_ipc
