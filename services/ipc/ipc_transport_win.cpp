// Windows (named pipe) implementation of the hostd IPC transport. The POSIX
// AF_UNIX backend lives in ipc_transport.cpp; both files are always listed in
// CMake and self-select via the #ifdef, so no per-platform source lists.
//
// Shape of the backend:
//   * Byte-mode duplex pipes, PIPE_REJECT_REMOTE_CLIENTS, unlimited instances.
//   * Overlapped reads/writes wait on distinct completion events and a sticky
//     shutdown event. Timeouts cancel and drain before stack buffers retire;
//     normal blocking reads do not poll the pipe or the system timer.
//   * Security (plan Top-risk "pipe squat/spoof"):
//       server: explicit DACL = current-user SID + SYSTEM only, plus
//               FILE_FLAG_FIRST_PIPE_INSTANCE on instance #1 so a squatter who
//               pre-created the name makes listen() FAIL LOUDLY instead of us
//               silently sharing the name;
//       client: after CreateFileW, resolves the server PID
//               (GetNamedPipeServerProcessId) and refuses to talk unless the
//               server process token's user SID equals our own (or SYSTEM).
#ifdef _WIN32

#include "ipc_transport.h"
#include "ipc_windows_local.h"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <aclapi.h>  // EXPLICIT_ACCESS_W, SetEntriesInAclW

#include <memory>
#include <vector>

namespace chudware_ipc {

namespace {

std::wstring widen(const std::string& s) {
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), w.data(), n);
    return w;
}

std::string winErr(const char* what, DWORD code) {
    char buf[256];
    DWORD n = FormatMessageA(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
                             nullptr, code, 0, buf, sizeof buf, nullptr);
    while (n > 0 && (buf[n - 1] == '\r' || buf[n - 1] == '\n')) buf[--n] = 0;
    return std::string(what) + ": " + (n ? buf : "error") + " (" + std::to_string(code) + ")";
}

// TOKEN_USER blob for a process token (caller owns the buffer).
std::vector<unsigned char> tokenUser(HANDLE process, std::string& err) {
    std::vector<unsigned char> out;
    HANDLE tok = nullptr;
    if (!OpenProcessToken(process, TOKEN_QUERY, &tok)) {
        err = winErr("OpenProcessToken", GetLastError());
        return out;
    }
    DWORD need = 0;
    GetTokenInformation(tok, TokenUser, nullptr, 0, &need);
    if (need == 0) {
        err = winErr("GetTokenInformation(size)", GetLastError());
        CloseHandle(tok);
        return out;
    }
    out.resize(need);
    if (!GetTokenInformation(tok, TokenUser, out.data(), need, &need)) {
        err = winErr("GetTokenInformation", GetLastError());
        out.clear();
    }
    CloseHandle(tok);
    return out;
}

PSID sidOf(std::vector<unsigned char>& tu) {
    return reinterpret_cast<TOKEN_USER*>(tu.data())->User.Sid;
}

enum class PipeIoResult { complete, timeout, closed };

PipeIoResult pipeIo(HANDLE pipe, HANDLE event, HANDLE stopped, bool write,
                    void* buffer, DWORD size, DWORD waitMs, DWORD& count) {
    count = 0;
    if (WaitForSingleObject(stopped, 0) == WAIT_OBJECT_0 || !ResetEvent(event))
        return PipeIoResult::closed;
    OVERLAPPED operation{};
    operation.hEvent = event;
    BOOL completed = write ? WriteFile(pipe, buffer, size, nullptr, &operation)
                           : ReadFile(pipe, buffer, size, nullptr, &operation);
    if (!completed) {
        if (GetLastError() != ERROR_IO_PENDING) return PipeIoResult::closed;
        const HANDLE events[] = {stopped, event};
        const DWORD waited = WaitForMultipleObjects(2, events, FALSE, waitMs);
        if (waited != WAIT_OBJECT_0 + 1) {
            CancelIoEx(pipe, &operation);
            // A completion can race the deadline. Preserve any consumed bytes,
            // and always drain before the OVERLAPPED or caller buffer retires.
            completed = GetOverlappedResult(pipe, &operation, &count, TRUE);
            const DWORD error = completed ? ERROR_SUCCESS : GetLastError();
            if (!completed) count = 0;
            if (waited == WAIT_TIMEOUT && (completed || error == ERROR_OPERATION_ABORTED))
                return PipeIoResult::timeout;
            return PipeIoResult::closed;
        }
    }
    completed = GetOverlappedResult(pipe, &operation, &count, FALSE);
    if (!completed) count = 0;
    return completed && count ? PipeIoResult::complete : PipeIoResult::closed;
}

}  // namespace

// ------------------------------------------------------------------ IpcConn

IpcConn::IpcConn(void* pipeHandle) {
    if (pipeHandle == INVALID_HANDLE_VALUE) pipeHandle = nullptr;
    h_ = pipeHandle;
    if (h_) {
        ev_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        writeEvent_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        shutdownEvent_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (!ev_ || !writeEvent_ || !shutdownEvent_) close();
    }
}

IpcConn& IpcConn::operator=(IpcConn&& o) noexcept {
    if (this != &o) {
        close();
        h_ = o.h_;
        ev_ = o.ev_;
        writeEvent_ = o.writeEvent_;
        shutdownEvent_ = o.shutdownEvent_;
        // std::atomic is not assignable — mirror the Linux path (store/load).
        eof_.store(o.eof_.load(std::memory_order_relaxed), std::memory_order_relaxed);
        readBuf_ = std::move(o.readBuf_);
        o.h_ = nullptr;
        o.ev_ = nullptr;
        o.writeEvent_ = nullptr;
        o.shutdownEvent_ = nullptr;
        o.eof_.store(false, std::memory_order_relaxed);
        o.readBuf_.clear();
    }
    return *this;
}

void IpcConn::close() {
    if (h_) {
        CancelIoEx((HANDLE)h_, nullptr);  // unblock any overlapped op in flight
        CloseHandle((HANDLE)h_);
        h_ = nullptr;
    }
    if (ev_) {
        CloseHandle((HANDLE)ev_);
        ev_ = nullptr;
    }
    if (writeEvent_) {
        CloseHandle((HANDLE)writeEvent_);
        writeEvent_ = nullptr;
    }
    if (shutdownEvent_) {
        CloseHandle((HANDLE)shutdownEvent_);
        shutdownEvent_ = nullptr;
    }
}

void IpcConn::shutdown() {
    // The owner closes only after its I/O threads join. A persistent signal
    // closes the race where CancelIoEx runs just before an operation is issued.
    eof_.store(true, std::memory_order_release);
    if (shutdownEvent_) SetEvent((HANDLE)shutdownEvent_);
    if (h_) CancelIoEx((HANDLE)h_, nullptr);
}

bool IpcConn::readLine(std::string& line, int timeoutMs) {
    if (!h_ || !ev_) return false;
    constexpr size_t kMaxLine = 64u * 1024u * 1024u;
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::milliseconds(std::max(0, timeoutMs));
    size_t scanFrom = 0;
    for (;;) {
        const size_t nl = readBuf_.find('\n', scanFrom);
        if ((nl != std::string::npos && nl > kMaxLine) ||
            (nl == std::string::npos && readBuf_.size() > kMaxLine)) {
            readBuf_.clear();
            shutdown();
            return false;
        }
        scanFrom = readBuf_.size();
        if (nl != std::string::npos) {
            line.assign(readBuf_.data(), nl);
            readBuf_.erase(0, nl + 1);
            return true;
        }
        if (eof_) return false;
        const DWORD wait = timeoutMs < 0 ? INFINITE
                                        : local_pipe_detail::remainingMs(deadline);
        if (timeoutMs > 0 && !wait) return false;
        char buffer[16384];
        DWORD want = static_cast<DWORD>(std::min(sizeof buffer, kMaxLine + 1 - readBuf_.size()));
        if (timeoutMs == 0) {
            DWORD available = 0;
            if (!PeekNamedPipe((HANDLE)h_, nullptr, 0, nullptr, &available, nullptr)) {
                eof_.store(true, std::memory_order_release);
                return false;
            }
            if (!available) return false;
            want = std::min(want, available);
        }
        DWORD got = 0;
        const auto result = pipeIo((HANDLE)h_, (HANDLE)ev_, (HANDLE)shutdownEvent_,
                                   false, buffer, want, wait, got);
        if (got) readBuf_.append(buffer, got);
        if (result == PipeIoResult::timeout) return false;
        if (result == PipeIoResult::closed) {
            eof_.store(true, std::memory_order_release);
            return false;
        }
    }
}

bool IpcConn::writeLine(const std::string& line) {
    if (!h_ || !writeEvent_ || eof_) return false;
    // Two views avoid allocating/copying the whole frame merely to append '\n'.
    const char newline = '\n';
    const char* segments[] = {line.data(), &newline};
    const size_t sizes[] = {line.size(), 1};
    for (size_t segment = 0; segment < 2; ++segment) {
        size_t offset = 0;
        while (offset < sizes[segment]) {
            DWORD wrote = 0;
            const DWORD size = static_cast<DWORD>(
                std::min<size_t>(sizes[segment] - offset, 65536));
            const auto result = pipeIo((HANDLE)h_, (HANDLE)writeEvent_, (HANDLE)shutdownEvent_,
                true, const_cast<char*>(segments[segment] + offset), size, INFINITE, wrote);
            if (result != PipeIoResult::complete) {
                eof_.store(true, std::memory_order_release);
                return false;
            }
            offset += wrote;
        }
    }
    return true;
}

// --------------------------------------------------------------- IpcListener

IpcListener& IpcListener::operator=(IpcListener&& o) noexcept {
    if (this != &o) {
        close();
        pending_ = o.pending_;
        ov_ = o.ov_;
        connectPending_ = o.connectPending_;
        dacl_ = o.dacl_;
        sd_ = o.sd_;
        path_ = std::move(o.path_);
        o.pending_ = nullptr;
        o.ov_ = nullptr;
        o.connectPending_ = false;
        o.dacl_ = nullptr;
        o.sd_ = nullptr;
        o.path_.clear();
    }
    return *this;
}

// Create the next pipe instance into pending_. `first` adds
// FILE_FLAG_FIRST_PIPE_INSTANCE (anti-squat: fails with ERROR_ACCESS_DENIED if
// the name already exists — squatter or live daemon, either way we must not
// serve on it).
bool IpcListener::createNextInstance(bool first, std::string& err) {
    SECURITY_ATTRIBUTES sa{};
    sa.nLength = sizeof sa;
    sa.lpSecurityDescriptor = sd_;
    sa.bInheritHandle = FALSE;
    DWORD openMode = PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED |
                     (first ? FILE_FLAG_FIRST_PIPE_INSTANCE : 0);
    HANDLE h = CreateNamedPipeW(widen(path_).c_str(), openMode,
                                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT |
                                    PIPE_REJECT_REMOTE_CLIENTS,
                                PIPE_UNLIMITED_INSTANCES,
                                /*out buffer*/ 64 * 1024, /*in buffer*/ 64 * 1024,
                                /*default timeout ms*/ 0, &sa);
    if (h == INVALID_HANDLE_VALUE) {
        DWORD e = GetLastError();
        err = (first && (e == ERROR_ACCESS_DENIED || e == ERROR_PIPE_BUSY))
                  ? "pipe name already exists (another daemon, or a squatter): " + path_
                  : winErr("CreateNamedPipeW", e);
        return false;
    }
    pending_ = h;
    connectPending_ = false;
    return true;
}

bool IpcListener::adoptListeningFd(int /*fd*/, std::string& err) {
    err = "Windows: inherited Unix listeners are unsupported";
    return false;
}

bool IpcListener::listen(const std::string& path, std::string& err) {
    close();
    path_ = path;

    // DACL: current user + SYSTEM, full access; nobody else (defence in depth on
    // top of the client-side server-SID check — pipes are machine-global names).
    // (SetEntriesInAclW copies the SIDs into the ACEs it builds, so these local
    // buffers don't need to outlive this function.)
    std::string terr;
    std::vector<unsigned char> tu = tokenUser(GetCurrentProcess(), terr);
    if (tu.empty()) {
        err = "pipe DACL: " + terr;
        path_.clear();
        return false;
    }
    std::vector<unsigned char> systemSid(SECURITY_MAX_SID_SIZE);
    DWORD sidLen = (DWORD)systemSid.size();
    if (!CreateWellKnownSid(WinLocalSystemSid, nullptr, systemSid.data(), &sidLen)) {
        err = winErr("CreateWellKnownSid(SYSTEM)", GetLastError());
        path_.clear();
        return false;
    }
    EXPLICIT_ACCESS_W ea[2]{};
    for (int i = 0; i < 2; ++i) {
        ea[i].grfAccessPermissions = GENERIC_READ | GENERIC_WRITE;
        ea[i].grfAccessMode = SET_ACCESS;
        ea[i].grfInheritance = NO_INHERITANCE;
        ea[i].Trustee.TrusteeForm = TRUSTEE_IS_SID;
    }
    ea[0].Trustee.TrusteeType = TRUSTEE_IS_USER;
    ea[0].Trustee.ptstrName = (LPWSTR)sidOf(tu);
    // The server itself needs FILE_CREATE_PIPE_INSTANCE for instances 2..N; it's
    // included in GENERIC_ALL, so the owner entry gets full control.
    ea[0].grfAccessPermissions = GENERIC_ALL;
    ea[1].Trustee.TrusteeType = TRUSTEE_IS_WELL_KNOWN_GROUP;
    ea[1].Trustee.ptstrName = (LPWSTR)systemSid.data();

    PACL pacl = nullptr;
    DWORD rc = SetEntriesInAclW(2, ea, nullptr, &pacl);
    if (rc != ERROR_SUCCESS) {
        err = winErr("SetEntriesInAclW", rc);
        path_.clear();
        return false;
    }
    dacl_ = pacl;
    auto* sd = (SECURITY_DESCRIPTOR*)LocalAlloc(LPTR, SECURITY_DESCRIPTOR_MIN_LENGTH);
    if (!sd || !InitializeSecurityDescriptor(sd, SECURITY_DESCRIPTOR_REVISION) ||
        !SetSecurityDescriptorDacl(sd, /*present*/ TRUE, pacl, /*defaulted*/ FALSE)) {
        err = winErr("SetSecurityDescriptorDacl", GetLastError());
        if (sd) LocalFree(sd);
        close();
        return false;
    }
    sd_ = sd;

    ov_ = LocalAlloc(LPTR, sizeof(OVERLAPPED));
    if (!ov_) {
        err = "OVERLAPPED alloc failed";
        close();
        return false;
    }
    ((OVERLAPPED*)ov_)->hEvent = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!((OVERLAPPED*)ov_)->hEvent) {
        err = winErr("CreateEventW", GetLastError());
        close();
        return false;
    }

    if (!createNextInstance(/*first=*/true, err)) {
        close();
        return false;
    }
    return true;
}

IpcConn IpcListener::accept(int timeoutMs) {
    if (!pending_ || !ov_) return IpcConn();
    OVERLAPPED* ov = (OVERLAPPED*)ov_;
    if (!connectPending_) {
        HANDLE ev = ov->hEvent;
        memset(ov, 0, sizeof *ov);
        ov->hEvent = ev;
        ResetEvent(ev);
        if (ConnectNamedPipe((HANDLE)pending_, ov)) {
            // Docs: in overlapped mode this "returns nonzero only in unusual
            // circumstances" — treat as connected.
            SetEvent(ev);
        } else {
            DWORD e = GetLastError();
            if (e == ERROR_PIPE_CONNECTED) {
                SetEvent(ev);  // client raced in between create and connect
            } else if (e == ERROR_IO_PENDING) {
                connectPending_ = true;
            } else if (e == ERROR_NO_DATA) {
                // A client opened and closed before accept issued the connect.
                // Without recycling, every subsequent accept hits NO_DATA and
                // the live first instance permanently loses admission capacity.
                DisconnectNamedPipe((HANDLE)pending_);
                return IpcConn();
            } else {
                return IpcConn();  // listener is likely broken; caller loops
            }
        }
    }
    DWORD wait = WaitForSingleObject(ov->hEvent,
                                     timeoutMs < 0 ? INFINITE : (DWORD)timeoutMs);
    if (wait != WAIT_OBJECT_0) return IpcConn();  // timeout: connect stays pending
    if (connectPending_) {
        DWORD ignored = 0;
        connectPending_ = false;
        if (!GetOverlappedResult((HANDLE)pending_, ov, &ignored, FALSE)) {
            // Failed connect (client vanished): recycle this instance.
            DisconnectNamedPipe((HANDLE)pending_);
            return IpcConn();
        }
    }
    IpcConn conn(pending_);
    pending_ = nullptr;
    std::string err;
    createNextInstance(/*first=*/false, err);  // failure -> valid()==false; run loop exits
    return conn;
}

std::string IpcListener::releasePath() {
    std::string p = std::move(path_);
    path_.clear();
    return p;
}

void IpcListener::close(bool /*unlink_path*/) {
    // Named pipes have no sock file to unlink; unlink_path is ignored on Windows.
    if (pending_ && ov_ && connectPending_) {
        DWORD ignored = 0;
        auto* operation = static_cast<OVERLAPPED*>(ov_);
        CancelIoEx(static_cast<HANDLE>(pending_), operation);
        // Closing the handle only requests cancellation too. The kernel must
        // finish before the heap OVERLAPPED and its event can be freed.
        GetOverlappedResult(static_cast<HANDLE>(pending_), operation, &ignored, TRUE);
    }
    if (pending_) {
        CloseHandle((HANDLE)pending_);
        pending_ = nullptr;
    }
    if (ov_) {
        if (((OVERLAPPED*)ov_)->hEvent) CloseHandle(((OVERLAPPED*)ov_)->hEvent);
        LocalFree(ov_);
        ov_ = nullptr;
    }
    if (sd_) {
        LocalFree(sd_);
        sd_ = nullptr;
    }
    if (dacl_) {
        LocalFree(dacl_);
        dacl_ = nullptr;
    }
    connectPending_ = false;
    path_.clear();
}

// ------------------------------------------------------------------ connectTo

IpcConn connectTo(const std::string& path, std::string& err) {
    const std::wstring wpath = widen(path);
    HANDLE h = INVALID_HANDLE_VALUE;
    for (int attempt = 0; attempt < 3; ++attempt) {
        h = CreateFileW(wpath.c_str(), GENERIC_READ | GENERIC_WRITE,
                        /*no sharing*/ 0, nullptr, OPEN_EXISTING, FILE_FLAG_OVERLAPPED,
                        nullptr);
        if (h != INVALID_HANDLE_VALUE) break;
        DWORD e = GetLastError();
        if (e != ERROR_PIPE_BUSY) {
            err = winErr(("connect " + path).c_str(), e);
            return IpcConn();
        }
        // All instances momentarily busy (unlimited instances makes this rare —
        // just the gap between accept and create-next). Wait briefly and retry.
        if (!WaitNamedPipeW(wpath.c_str(), 1000)) {
            err = winErr(("WaitNamedPipeW " + path).c_str(), GetLastError());
            return IpcConn();
        }
    }
    if (h == INVALID_HANDLE_VALUE) {
        err = "pipe busy: " + path;
        return IpcConn();
    }

    // Shared anti-spoof rule: capability/document bytes go only to our owner or SYSTEM.
    if (!trustedPipeServer(h)) {
        err = "pipe server identity mismatch";
        CloseHandle(h);
        return IpcConn();
    }
    return IpcConn((void*)h);
}

}  // namespace chudware_ipc

#endif  // _WIN32
