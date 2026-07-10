// IPC transport for the shared engine-host daemon (aicad-hostd).
//
// Newline-delimited JSON-RPC over a local stream transport — the same framing the
// Python-worker seam uses (services/worker/worker_client.cpp): one JSON object per
// line, `{"id":N,"method":M,"params":P}` requests, `{"id":N,"result":R|"error":E}`
// responses, plus id-less `{"event":...,"data":...}` push lines.
//
// Linux: AF_UNIX SOCK_STREAM at $XDG_RUNTIME_DIR/aicad/hostd-<key>.sock
//   (ipc_transport.cpp)
// Windows: named pipe \\.\pipe\aicad-hostd-<SID>-<key>, byte mode, overlapped
//   handles, same-user DACL (ipc_transport_win.cpp)
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
#ifdef _WIN32
    // Takes ownership of a connected pipe HANDLE opened with FILE_FLAG_OVERLAPPED
    // (both accept() and connectTo() produce those). void* to keep <windows.h>
    // out of this header.
    explicit IpcConn(void* pipeHandle);
#else
    explicit IpcConn(int fd) : fd_(fd) {}
#endif
    ~IpcConn() { close(); }
    IpcConn(IpcConn&& o) noexcept { *this = std::move(o); }
    IpcConn& operator=(IpcConn&& o) noexcept;
    IpcConn(const IpcConn&) = delete;
    IpcConn& operator=(const IpcConn&) = delete;

#ifdef _WIN32
    bool valid() const { return h_ != nullptr; }
#else
    bool valid() const { return fd_ >= 0; }
#endif

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
#ifndef _WIN32
    int fd() const { return fd_; }
#endif

private:
#ifdef _WIN32
    void* h_ = nullptr;   // pipe HANDLE (nullptr = invalid; INVALID_HANDLE_VALUE normalized away)
    void* ev_ = nullptr;  // manual-reset event backing this conn's OVERLAPPED reads/writes
#else
    int fd_ = -1;
#endif
    bool eof_ = false;
    std::string readBuf_;
};

// Listening endpoint (daemon side).
class IpcListener {
public:
    IpcListener() = default;
    ~IpcListener() { close(); }
    IpcListener(IpcListener&& o) noexcept { *this = std::move(o); }
    IpcListener& operator=(IpcListener&& o) noexcept;
    IpcListener(const IpcListener&) = delete;
    IpcListener& operator=(const IpcListener&) = delete;

    // POSIX: bind + listen on the unix socket at `path`, creating parent dirs
    // (0700). An existing socket file is probe-connected first: live daemon ->
    // fail with "already running"; stale file -> unlinked (caller must hold the
    // single-instance lock so this is race-free).
    // Windows: `path` is the \\.\pipe\... name; creates the first pipe instance
    // with FILE_FLAG_FIRST_PIPE_INSTANCE (anti-squat: fails if the name exists)
    // and a same-user-SID + SYSTEM DACL, PIPE_REJECT_REMOTE_CLIENTS.
    bool listen(const std::string& path, std::string& err);

    // Accept one connection. timeoutMs < 0 blocks forever. Returns an invalid
    // conn on timeout/error/closed listener. (Windows: an overlapped
    // ConnectNamedPipe stays pending across timed-out calls; on completion the
    // instance handle is handed to the returned IpcConn and the next instance
    // is created immediately.)
    IpcConn accept(int timeoutMs);

    void close();
#ifdef _WIN32
    bool valid() const { return pending_ != nullptr; }
#else
    bool valid() const { return fd_ >= 0; }
#endif

private:
#ifdef _WIN32
    bool createNextInstance(bool first, std::string& err);
    void* pending_ = nullptr;    // next pipe instance HANDLE awaiting a client
    void* ov_ = nullptr;         // heap OVERLAPPED* (stable across accept calls)
    bool connectPending_ = false;
    void* dacl_ = nullptr;       // PACL from SetEntriesInAclW (LocalFree'd on close)
    void* sd_ = nullptr;         // SECURITY_DESCRIPTOR*, references dacl_
#else
    int fd_ = -1;
#endif
    std::string path_;
};

// Client-side connect. Returns an invalid conn (err filled) if nothing listens.
IpcConn connectTo(const std::string& path, std::string& err);

}  // namespace aicad_ipc
