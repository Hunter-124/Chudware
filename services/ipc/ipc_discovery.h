// Daemon discovery + lifecycle primitives shared by chudware-hostd and every client
// (GUI DaemonConn, Python daemon_bridge reimplements the same contract).
//
// Developer builds may override instance/socket/binary discovery for tests and
// source-tree workflows. Production derives identity from the canonical adjacent
// hostd and ignores those overrides.
//
// The key is 16 hex chars of FNV-1a 64. It is namespacing, not authentication;
// the owner-private runtime/state directory or pipe ACL supplies isolation.

#include <string>

namespace chudware_ipc {

// FNV-1a 64 of `s`, as 16 lowercase hex chars.
std::string fnv1aHex(const std::string& s);

// The instance key for a given hostd binary path ("" = resolve via env only).
std::string instanceKey(const std::string& hostdBin);

// Socket + lock paths for `key`. Both live in the per-user runtime dir.
std::string socketPath(const std::string& key);
std::string lockPath(const std::string& key);

// Single-instance lock name for the DESKTOP GUI, keyed the same way (instanceKey of the
// GUI binary). A sibling of lockPath() that can never collide with it: the daemon and the
// window are independent singletons — one daemon may legitimately outlive/precede its GUI.
// POSIX -> a sibling runtime lock file; Windows -> a session-scoped private state
// lock file. Both are handle-lifetime claims, never public named mutex objects.
std::string guiLockPath(const std::string& key);

// Runtime dir ($XDG_RUNTIME_DIR/chudware or /tmp/chudware-<uid>), created 0700 with
// ownership verified (refuses a dir owned by someone else). "" on failure.
std::string runtimeDir();

// State dir for logs/autosaves, created/validated owner-private; "" on failure.
std::string stateDir();

// Resolve the hostd binary. Developer builds honor $CHUDWARE_HOSTD_BIN then `hint`.
// Production ignores both and returns only the canonical adjacent hostd after its exact
// signed payload-manifest row is authenticated; otherwise returns "".
std::string resolveHostdBin(const std::string& hint);

// Single-instance guard: flock on POSIX, restricted-sharing private file handle
// on Windows. Held for process lifetime; death releases the claim even though
// the harmless diagnostic lock file remains.
class SingleInstance {
public:
    ~SingleInstance() { release(); }
    // Try to take the lock (non-blocking). False = another daemon holds it.
    bool acquire(const std::string& lockFile, std::string& err);
    void release();
    // Sock path to unlink when the flock is released (so the sock
    // file never disappears while the lock is still held). Empty = no-op.
    void setSockPathToUnlink(const std::string& sockPath) { sock_path_ = sockPath; }
#ifdef _WIN32
    bool held() const { return h_ != nullptr; }
#else
    bool held() const { return fd_ >= 0; }
#endif

private:
#ifdef _WIN32
    void* h_ = nullptr;  // private lock FILE* (owns its restricted-sharing HANDLE)
#else
    int fd_ = -1;
#endif
    std::string path_;
    std::string sock_path_;  // deferred unlink (drain-window)
};

// Spawn `argv` fully detached (double-fork + setsid, fds to /dev/null) so the
// daemon outlives the spawning client. Returns false + err on failure. This is
// the replacement for the weak std::system() spawn in host/src/bootstrap.cpp.
bool spawnDetached(const std::string& exe, const std::string& arg, std::string& err);

}  // namespace chudware_ipc
