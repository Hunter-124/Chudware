// Windows local IPC helpers shared by worker control, daemon transport and GUI.
#pragma once
#ifdef _WIN32
#include "ipc_private.h"
#include <algorithm>
#include <chrono>
#include <cstring>

namespace chudware_ipc {
inline std::vector<unsigned char> localProcessUser(HANDLE process) {
    HANDLE token = nullptr;
    if (!OpenProcessToken(process, TOKEN_QUERY, &token)) return {};
    DWORD size = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &size);
    std::vector<unsigned char> user;
    if (size && size <= 65536) {
        user.resize(size);
        if (!GetTokenInformation(token, TokenUser, user.data(), size, &size)) user.clear();
    }
    CloseHandle(token);
    return user;
}
inline PSID localUserSid(const std::vector<unsigned char>& user) {
    return user.empty() ? nullptr : reinterpret_cast<const TOKEN_USER*>(user.data())->User.Sid;
}

// Existing daemon anti-spoof rule: do not send capability bytes until the pipe
// server's process token has our user SID (or SYSTEM).
inline bool trustedPipeServer(HANDLE pipe) {
    ULONG pid = 0;
    if (!GetNamedPipeServerProcessId(pipe, &pid)) return false;
    HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!process) return false;
    const auto theirs = localProcessUser(process);
    CloseHandle(process);
    const auto ours = localProcessUser(GetCurrentProcess());
    const PSID server = localUserSid(theirs), owner = localUserSid(ours);
    return server && owner && (EqualSid(server, owner) || IsWellKnownSid(server, WinLocalSystemSid));
}

// Availability only: a foreign or permissively-ACL'd preexisting event is never
// accepted as a wake channel, and never establishes singleton authority.
inline bool ownerPrivateEvent(HANDLE event) {
    PSID owner = nullptr;
    PACL acl = nullptr;
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    if (GetSecurityInfo(event, SE_KERNEL_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &owner, nullptr, &acl, nullptr, &descriptor) != ERROR_SUCCESS) return false;
    const auto ours = localProcessUser(GetCurrentProcess());
    SECURITY_DESCRIPTOR_CONTROL control = 0;
    DWORD revision = 0;
    bool ok = owner && localUserSid(ours) && EqualSid(owner, localUserSid(ours)) && acl &&
              acl->AceCount == 2 && GetSecurityDescriptorControl(descriptor, &control, &revision) &&
              (control & SE_DACL_PROTECTED);
    bool foundOwner = false, foundSystem = false;
    for (DWORD i = 0; ok && i < acl->AceCount; ++i) {
        void* raw = nullptr;
        if (!GetAce(acl, i, &raw)) { ok = false; break; }
        const auto* ace = static_cast<const ACCESS_ALLOWED_ACE*>(raw);
        if (ace->Header.AceType != ACCESS_ALLOWED_ACE_TYPE ||
            (ace->Header.AceFlags & INHERIT_ONLY_ACE) ||
            (ace->Mask & EVENT_ALL_ACCESS) != EVENT_ALL_ACCESS) { ok = false; break; }
        PSID sid = const_cast<DWORD*>(&ace->SidStart);
        if (EqualSid(sid, owner)) foundOwner = true;
        else if (IsWellKnownSid(sid, WinLocalSystemSid)) foundSystem = true;
        else ok = false;
    }
    LocalFree(descriptor);
    return ok && foundOwner && foundSystem;
}
inline HANDLE privateActivationEvent(const std::wstring& name, bool create) {
    auto& security = privateSecurity();
    if (!security.valid()) return nullptr;
    constexpr DWORD access = SYNCHRONIZE | EVENT_MODIFY_STATE | READ_CONTROL;
    HANDLE event = create ? CreateEventExW(&security.attributes, name.c_str(), 0, access)
                          : OpenEventW(access, FALSE, name.c_str());
    if (event && !ownerPrivateEvent(event)) { CloseHandle(event); return nullptr; }
    return event;
}
inline bool privateLockHeld(const std::string& path) {
    HANDLE probe = CreateFileW(std::filesystem::u8path(path).c_str(), GENERIC_WRITE,
                              FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                              FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (probe != INVALID_HANDLE_VALUE) { CloseHandle(probe); return false; }
    return GetLastError() == ERROR_SHARING_VIOLATION;
}

namespace local_pipe_detail {
class Handle {
public:
    explicit Handle(HANDLE h) : h_(h) {}
    ~Handle() { if (h_ && h_ != INVALID_HANDLE_VALUE) CloseHandle(h_); }
    Handle(const Handle&) = delete;
    Handle& operator=(const Handle&) = delete;
    HANDLE get() const { return h_; }
private:
    HANDLE h_;
};
using Deadline = std::chrono::steady_clock::time_point;
inline DWORD remainingMs(const Deadline& deadline) {
    const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
        deadline - std::chrono::steady_clock::now()).count();
    return remaining <= 0 ? 0 : static_cast<DWORD>(std::min<std::int64_t>(remaining, 0x7fffffff));
}
inline bool transfer(HANDLE pipe, HANDLE event, bool write, void* buffer, DWORD size,
                     const Deadline& deadline, DWORD& count, std::string& err) {
    if (!remainingMs(deadline)) { err = "worker control request timed out"; return false; }
    if (!ResetEvent(event)) { err = "worker control pipe event unavailable"; return false; }
    OVERLAPPED operation{};
    operation.hEvent = event;
    count = 0;
    BOOL completed = write ? WriteFile(pipe, buffer, size, &count, &operation)
                           : ReadFile(pipe, buffer, size, &count, &operation);
    DWORD error = completed ? ERROR_SUCCESS : GetLastError();
    if (error == ERROR_IO_PENDING) {
        const DWORD wait = WaitForSingleObject(event, remainingMs(deadline));
        if (wait != WAIT_OBJECT_0) {
            // CancelIoEx only requests cancellation. Do not retire the stack
            // OVERLAPPED, buffer or event until Windows reports completion.
            CancelIoEx(pipe, &operation);
            GetOverlappedResult(pipe, &operation, &count, TRUE);
            err = wait == WAIT_TIMEOUT ? "worker control request timed out"
                                       : "worker control pipe wait failed";
            return false;
        }
        completed = GetOverlappedResult(pipe, &operation, &count, FALSE);
    }
    if (completed && count) return true;
    err = "worker control pipe closed";
    return false;
}
}  // namespace local_pipe_detail

inline bool exchangeLocalPipe(const std::string& endpoint, const std::string& request,
                              std::string& response, std::size_t responseLimit,
                              int timeoutMs, std::string& err) {
    using namespace local_pipe_detail;
    if (timeoutMs <= 0 || endpoint.rfind("\\\\.\\pipe\\", 0) != 0) {
        err = "worker control pipe endpoint is invalid";
        return false;
    }
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeoutMs);
    const auto path = std::filesystem::u8path(endpoint);
    HANDLE pipe = INVALID_HANDLE_VALUE;
    while (remainingMs(deadline)) {
        pipe = CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                          OPEN_EXISTING, FILE_FLAG_OVERLAPPED |
                          SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, nullptr);
        if (pipe != INVALID_HANDLE_VALUE) break;
        if (GetLastError() != ERROR_PIPE_BUSY) {
            err = "worker control pipe connection failed";
            return false;
        }
        const DWORD remaining = remainingMs(deadline);
        if (remaining) WaitNamedPipeW(path.c_str(), remaining);
    }
    if (pipe == INVALID_HANDLE_VALUE) { err = "worker control request timed out"; return false; }
    Handle connection(pipe);
    if (!trustedPipeServer(pipe)) { err = "worker control pipe server identity mismatch"; return false; }
    Handle event(CreateEventW(nullptr, TRUE, FALSE, nullptr));
    if (!event.get()) { err = "worker control pipe event unavailable"; return false; }
    DWORD mode = PIPE_READMODE_BYTE | PIPE_WAIT;
    if (!SetNamedPipeHandleState(pipe, &mode, nullptr, nullptr)) {
        err = "worker control pipe state unavailable";
        return false;
    }
    std::size_t sent = 0;
    while (sent < request.size()) {
        DWORD count = 0;
        const DWORD size = static_cast<DWORD>(std::min<std::size_t>(request.size() - sent, 65536));
        if (!transfer(pipe, event.get(), true, const_cast<char*>(request.data() + sent),
                      size, deadline, count, err)) return false;
        sent += count;
    }
    response.clear();
    char buffer[4096];
    for (;;) {
        DWORD count = 0;
        if (!transfer(pipe, event.get(), false, buffer, sizeof buffer, deadline, count, err)) return false;
        const auto* newline = static_cast<const char*>(std::memchr(buffer, '\n', count));
        const std::size_t size = newline ? static_cast<std::size_t>(newline - buffer) : count;
        if (size > responseLimit || response.size() > responseLimit - size) {
            err = "worker control response exceeded limit";
            return false;
        }
        response.append(buffer, size);
        if (newline) return true;
    }
}
}  // namespace chudware_ipc
#endif
