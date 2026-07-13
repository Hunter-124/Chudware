// Windows (named pipe) implementation of the hostd IPC transport. The POSIX
// AF_UNIX backend lives in ipc_transport.cpp; both files are always listed in
// CMake and self-select via the #ifdef, so no per-platform source lists.
//
// WRITTEN ON LINUX, NEVER COMPILED HERE — every Win32 call below was checked
// against its documented signature, but this file is UNVERIFIED until the
// manual Windows checklist in docs/PACKAGING_WINDOWS.md runs on a real box.
//
// Shape of the backend:
//   * Byte-mode duplex pipes, PIPE_REJECT_REMOTE_CLIENTS, unlimited instances.
//   * All handles are opened FILE_FLAG_OVERLAPPED. Reads/writes still LOOK
//     synchronous to the caller: writeLine blocks in GetOverlappedResult;
//     readLine only issues ReadFile after PeekNamedPipe reports bytes, so the
//     overlapped op completes immediately (this mirrors the proven
//     WorkerClient::readLine polling pattern in services/worker/worker_client.cpp).
//     Overlapped mode exists for ONE reason: accept() needs a timeout, and a
//     timed ConnectNamedPipe is only possible overlapped.
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

namespace aicad_ipc {

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

}  // namespace

// ------------------------------------------------------------------ IpcConn

IpcConn::IpcConn(void* pipeHandle) {
    if (pipeHandle == INVALID_HANDLE_VALUE) pipeHandle = nullptr;
    h_ = pipeHandle;
    if (h_) ev_ = CreateEventW(nullptr, /*manual reset*/ TRUE, FALSE, nullptr);
}

IpcConn& IpcConn::operator=(IpcConn&& o) noexcept {
    if (this != &o) {
        close();
        h_ = o.h_;
        ev_ = o.ev_;
        // std::atomic is not assignable — mirror the Linux path (store/load).
        eof_.store(o.eof_.load(std::memory_order_relaxed), std::memory_order_relaxed);
        readBuf_ = std::move(o.readBuf_);
        o.h_ = nullptr;
        o.ev_ = nullptr;
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
}

bool IpcConn::readLine(std::string& line, int timeoutMs) {
    if (!h_ || !ev_) return false;
    const ULONGLONG start = GetTickCount64();
    for (;;) {
        size_t nl = readBuf_.find('\n');
        if (nl != std::string::npos) {
            line = readBuf_.substr(0, nl);
            readBuf_.erase(0, nl + 1);
            return true;
        }
        if (eof_) return false;
        if (timeoutMs >= 0 && (int)(GetTickCount64() - start) > timeoutMs) return false;

        DWORD avail = 0;
        if (!PeekNamedPipe((HANDLE)h_, nullptr, 0, nullptr, &avail, nullptr)) {
            eof_ = true;  // ERROR_BROKEN_PIPE etc.: peer is gone
            return false;
        }
        if (avail == 0) {
            if (timeoutMs == 0) return false;  // non-blocking probe
            Sleep(1);
            continue;
        }
        char buf[16384];
        DWORD want = avail < (DWORD)sizeof buf ? avail : (DWORD)sizeof buf;
        OVERLAPPED ov{};
        ov.hEvent = (HANDLE)ev_;
        DWORD got = 0;
        // Data is already buffered (peeked), so this completes immediately; the
        // OVERLAPPED is still mandatory because the handle is overlapped-mode.
        if (!ReadFile((HANDLE)h_, buf, want, nullptr, &ov) &&
            GetLastError() != ERROR_IO_PENDING) {
            eof_ = true;
            return false;
        }
        if (!GetOverlappedResult((HANDLE)h_, &ov, &got, /*bWait=*/TRUE) || got == 0) {
            eof_ = true;
            return false;
        }
        readBuf_.append(buf, got);
    }
}

bool IpcConn::writeLine(const std::string& line) {
    if (!h_ || !ev_) return false;
    std::string out = line;
    out.push_back('\n');
    size_t off = 0;
    while (off < out.size()) {
        OVERLAPPED ov{};
        ov.hEvent = (HANDLE)ev_;
        DWORD wrote = 0;
        if (!WriteFile((HANDLE)h_, out.data() + off, (DWORD)(out.size() - off), nullptr,
                       &ov) &&
            GetLastError() != ERROR_IO_PENDING) {
            eof_ = true;
            return false;
        }
        if (!GetOverlappedResult((HANDLE)h_, &ov, &wrote, /*bWait=*/TRUE) || wrote == 0) {
            eof_ = true;
            return false;
        }
        off += wrote;
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

    // Anti-spoof (plan Top-risk 2): only talk to a server running as us (or
    // SYSTEM). A squatter re-creating the name after our daemon exits would
    // otherwise receive our doc contents / agent_spawn commands.
    ULONG serverPid = 0;
    std::string terr;
    if (!GetNamedPipeServerProcessId(h, &serverPid)) {
        err = winErr("GetNamedPipeServerProcessId", GetLastError());
        CloseHandle(h);
        return IpcConn();
    }
    HANDLE sp = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, serverPid);
    if (!sp) {
        err = winErr("OpenProcess(pipe server)", GetLastError());
        CloseHandle(h);
        return IpcConn();
    }
    std::vector<unsigned char> mine = tokenUser(GetCurrentProcess(), terr);
    std::vector<unsigned char> theirs = tokenUser(sp, terr);
    CloseHandle(sp);
    bool ok = false;
    if (!mine.empty() && !theirs.empty()) {
        PSID s = sidOf(theirs);
        ok = EqualSid(sidOf(mine), s) || IsWellKnownSid(s, WinLocalSystemSid);
    }
    if (!ok) {
        err = terr.empty() ? "pipe server identity mismatch (not our user): " + path
                           : "pipe server identity check failed: " + terr;
        CloseHandle(h);
        return IpcConn();
    }
    return IpcConn((void*)h);
}

}  // namespace aicad_ipc

#endif  // _WIN32
