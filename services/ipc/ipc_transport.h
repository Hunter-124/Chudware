// IPC transport for the shared engine-host daemon (aicad-hostd).
//
// Newline-delimited JSON-RPC over a local stream transport — the same framing the
// Python-worker seam uses (services/worker/worker_client.cpp): one JSON object per
// line, `{"id":N,"method":M,"params":P}` requests, `{"id":N,"result":R|"error":E}`
// responses, plus id-less `{"event":...,"data":...}` push lines.
//
// Linux: AF_UNIX SOCK_STREAM at $XDG_RUNTIME_DIR/aicad/hostd-<key>.sock
// Windows (Phase 6): named pipe \\.\pipe\aicad-hostd-<SID>-<key>
//
// This layer is transport only — no JSON parsing, no protocol. Both the daemon
// (core/hostd) and every C++ client (GUI DaemonConn, tests) compile these sources.
#pragma once

#include <string>

namespace aicad_ipc {

// A connected stream. Move-only; closes on destruction.
class IpcConn {
public:
    IpcConn() = default;
    explicit IpcConn(int fd) : fd_(fd) {}
    ~IpcConn() { close(); }
    IpcConn(IpcConn&& o) noexcept { *this = std::move(o); }
    IpcConn& operator=(IpcConn&& o) noexcept;
    IpcConn(const IpcConn&) = delete;
    IpcConn& operator=(const IpcConn&) = delete;

    bool valid() const { return fd_ >= 0; }

    // Read one \n-terminated line (without the \n). timeoutMs < 0 blocks forever;
    // timeoutMs == 0 is a non-blocking poll (returns false immediately if no full
    // line is buffered/readable). Returns false on timeout, EOF, or error — use
    // eof() to distinguish a dead peer from a timeout.
    bool readLine(std::string& line, int timeoutMs);

    // Write `line` + '\n' fully. Serialized by the CALLER (per-conn write mutex
    // lives in the protocol layer, where response writes and event pushes meet).
    bool writeLine(const std::string& line);

    bool eof() const { return eof_; }
    void close();
    int fd() const { return fd_; }

private:
    int fd_ = -1;
    bool eof_ = false;
    std::string readBuf_;
};

// Listening endpoint (daemon side).
class IpcListener {
public:
    IpcListener() = default;
    ~IpcListener() { close(); }
    IpcListener(IpcListener&& o) noexcept { *this = std::move(o); }
    IpcListener& operator=(IpcListener&& o) noexcept {
        if (this != &o) {
            close();
            fd_ = o.fd_;
            path_ = std::move(o.path_);
            o.fd_ = -1;
            o.path_.clear();
        }
        return *this;
    }
    IpcListener(const IpcListener&) = delete;
    IpcListener& operator=(const IpcListener&) = delete;

    // Bind + listen on the unix socket at `path`, creating parent dirs (0700).
    // An existing socket file is probe-connected first: live daemon -> fail with
    // "already running"; stale file -> unlinked (caller must hold the single-
    // instance lock so this is race-free).
    bool listen(const std::string& path, std::string& err);

    // Accept one connection. timeoutMs < 0 blocks forever. Returns an invalid
    // conn on timeout/error/closed listener.
    IpcConn accept(int timeoutMs);

    void close();
    bool valid() const { return fd_ >= 0; }

private:
    int fd_ = -1;
    std::string path_;
};

// Client-side connect. Returns an invalid conn (err filled) if nothing listens.
IpcConn connectTo(const std::string& path, std::string& err);

}  // namespace aicad_ipc
