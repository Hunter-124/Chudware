// IPC transport for the shared engine-host daemon (chudware-hostd).
//
// Newline-delimited JSON-RPC over a local stream transport — the same framing the
// Python-worker seam uses (services/worker/worker_client.cpp): one JSON object per
// line, `{"id":N,"method":M,"params":P}` requests, `{"id":N,"result":R|"error":E}`
// responses, plus id-less `{"event":...,"data":...}` push lines.
//
// Linux: AF_UNIX SOCK_STREAM at $XDG_RUNTIME_DIR/chudware/hostd-<key>.sock
//   (ipc_transport.cpp)
// Windows: named pipe \\.\pipe\chudware-hostd-<SID>-<key>, byte mode, overlapped
//   handles, same-user DACL (ipc_transport_win.cpp)
// EMSCRIPTEN: WebSocket to the gateway `WS /engine` (ipc_transport_wasm.cpp);
//   path arg ignored; dials ws(s)://location.host/engine. Requires -lwebsocket.js.
//
// This layer is transport only — no JSON parsing, no protocol. Both the daemon
// (core/hostd) and every C++ client (GUI DaemonConn, tests) compile these sources.
#pragma once

#include <atomic>
#include <memory>
#include <string>

namespace chudware_ipc {

// A connected stream. Move-only; closes on destruction.
class IpcConn {
public:
    IpcConn() = default;
#ifdef _WIN32
    // Takes ownership of a connected pipe HANDLE opened with FILE_FLAG_OVERLAPPED
    // (both accept() and connectTo() produce those). void* to keep <windows.h>
    // out of this header.
    explicit IpcConn(void* pipeHandle);
#elif defined(__EMSCRIPTEN__)
    // Constructed only by connectTo() after the WebSocket handshake completes.
    // WasmState is heap-shared so WS callbacks (foreign thread/context) can append
    // to the line buffer under a mutex without pinning a moved-from IpcConn.
    struct WasmState;
    explicit IpcConn(std::shared_ptr<WasmState> ws);
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
#elif defined(__EMSCRIPTEN__)
    bool valid() const;
#else
    bool valid() const { return fd_ >= 0; }
#endif

    // Read one \n-terminated line (without the \n). Lines longer than 64 MiB
    // fail closed. timeoutMs < 0 blocks forever; timeoutMs == 0 is a
    // non-blocking poll. Returns false on timeout, EOF, or error — use eof()
    // to distinguish a dead peer from a timeout.
    bool readLine(std::string& line, int timeoutMs);

    // Write `line` + '\n' fully. Serialized by the CALLER (per-conn write mutex
    // lives in the protocol layer, where response writes and event pushes meet).
    bool writeLine(const std::string& line);

#ifdef __EMSCRIPTEN__
    bool eof() const;
#else
    bool eof() const { return eof_.load(std::memory_order_acquire); }
#endif
    void close();
    // Wake a peer thread blocked in readLine() WITHOUT closing the handle:
    // POSIX shutdown / Windows sticky cancellation plus CancelIoEx ensure a blocking
    // readLine returns promptly. The daemon shutdown path calls this before join()ing
    // the per-client threads — close() alone does NOT interrupt a thread already parked
    // in poll() on Linux, which hung ~Server to the systemd stop timeout (SIGABRT).
    void shutdown();
#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
    int fd() const { return fd_; }
#endif

private:
#ifdef _WIN32
    void* h_ = nullptr;   // pipe HANDLE (nullptr = invalid; INVALID_HANDLE_VALUE normalized away)
    void* ev_ = nullptr;  // read completion; reads and writes may run concurrently
    void* writeEvent_ = nullptr;
    void* shutdownEvent_ = nullptr;  // sticky cancellation, including before I/O starts
#elif defined(__EMSCRIPTEN__)
    std::shared_ptr<WasmState> ws_;
#else
    int fd_ = -1;
#endif
#ifndef __EMSCRIPTEN__
    // Written by writeLine on a foreign thread (event fanout) when a bounded
    // write fails, read by readLine on the owning client-loop thread — atomic so
    // that cross-thread signal is a clean handoff rather than a data race.
    std::atomic<bool> eof_{false};
    std::string readBuf_;
#endif
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

    // Takes ownership of an inherited AF_UNIX SOCK_STREAM listening fd, including
    // on failure. Never owns/unlinks its filesystem endpoint. POSIX only.
    bool adoptListeningFd(int fd, std::string& err);

    // Accept one connection. timeoutMs < 0 blocks forever. Returns an invalid
    // conn on timeout/error/closed listener. (Windows: an overlapped
    // ConnectNamedPipe stays pending across timed-out calls; on completion the
    // instance handle is handed to the returned IpcConn and the next instance
    // is created immediately.)
    IpcConn accept(int timeoutMs);

    // Close the listen fd. By default also unlinks the socket path. Pass
    // unlink_path=false to leave the sock file for a coordinated cleanup (T-PERF10:
    // hostd defers unlink until the single-instance flock is released so clients
    // never see lock-held-but-no-sock during drain).
    void close(bool unlink_path = true);

    // Detach the bound path without unlinking or closing. Returns the path (or
    // empty). After this, close() will not unlink. Used by hostd to hand the
    // sock path to SingleInstance for unlink-at-flock-release.
    std::string releasePath();
#ifdef _WIN32
    bool valid() const { return pending_ != nullptr; }
#elif defined(__EMSCRIPTEN__)
    // Browser client never listens — always invalid (server-side stubs in wasm TU).
    bool valid() const { return false; }
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
#elif !defined(__EMSCRIPTEN__)
    int fd_ = -1;
#endif
    std::string path_;
};

// Client-side connect. Returns an invalid conn (err filled) if nothing listens.
IpcConn connectTo(const std::string& path, std::string& err);

}  // namespace chudware_ipc
