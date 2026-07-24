// Windows implementation of daemon discovery/lifecycle (POSIX sibling:
// ipc_discovery.cpp). WRITTEN ON LINUX, NEVER COMPILED HERE — unverified until
// the manual checklist in docs/PACKAGING_WINDOWS.md runs on a real box.
//
// The function names stay transport-neutral on purpose:
//   socketPath(key) -> \\.\pipe\chudware-hostd-<SID>-<key>   (a pipe NAME, not a file)
//   lockPath(key)   -> Global\chudware-hostd-<SID>-<key>     (a named-MUTEX name)
// so hostd/main.cpp and ipc_client.cpp compile unchanged on both platforms.
//
// The user SID in both names gives per-user isolation for free (pipe and mutex
// namespaces are machine-global); the instance key keeps dev-tree and installed
// daemons apart, exactly like the POSIX socket path scheme.
#ifdef _WIN32

#include "ipc_discovery.h"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <sddl.h>  // ConvertSidToStringSidW

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cwctype>
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

std::string narrow(const std::wstring& w) {
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0,
                                nullptr, nullptr);
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), s.data(), n, nullptr,
                        nullptr);
    return s;
}

std::string lastErr(const char* what) {
    return std::string(what) + " failed (" + std::to_string(GetLastError()) + ")";
}

// Current user's SID as "S-1-5-21-..." (cached — it cannot change in-process).
const std::string& currentUserSid() {
    static const std::string sid = [] {
        std::string out = "S-unknown";
        HANDLE tok = nullptr;
        if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &tok)) return out;
        DWORD need = 0;
        GetTokenInformation(tok, TokenUser, nullptr, 0, &need);
        std::vector<unsigned char> buf(need ? need : 1);
        if (need &&
            GetTokenInformation(tok, TokenUser, buf.data(), need, &need)) {
            LPWSTR str = nullptr;
            if (ConvertSidToStringSidW(
                    reinterpret_cast<TOKEN_USER*>(buf.data())->User.Sid, &str)) {
                out = narrow(str);
                LocalFree(str);
            }
        }
        CloseHandle(tok);
        return out;
    }();
    return sid;
}

}  // namespace

std::string fnv1aHex(const std::string& s) {
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
    if (const char* k = std::getenv("CHUDWARE_INSTANCE_KEY"); k && *k) return k;
    std::string id = hostdBin;
    if (!id.empty()) {
        // Canonicalize: GetFullPathNameW (the realpath analog for identity —
        // resolves relative paths / . / ..) + lowercase (NTFS names are
        // case-insensitive; the same binary must hash to the same key however
        // the caller spelled it).
        std::wstring w = widen(id);
        wchar_t full[MAX_PATH];
        DWORD n = GetFullPathNameW(w.c_str(), MAX_PATH, full, nullptr);
        if (n > 0 && n < MAX_PATH) w.assign(full, n);
        for (auto& c : w) c = towlower(c);
        id = narrow(w);
    }
    if (id.empty()) id = "default";
    return fnv1aHex(id);
}

std::string runtimeDir() {
    // No runtime-file directory is needed on Windows (pipes and mutexes are
    // kernel names, not files); alias to the state dir for any generic caller.
    return stateDir();
}

std::string stateDir() {
    if (const char* a = std::getenv("APPDATA"); a && *a)
        return std::string(a) + "\\Chudware";
    return "Chudware";  // last resort: relative to cwd (matches bootstrap fallback spirit)
}

std::string socketPath(const std::string& key) {
    // Full-name overrides, checked in the same order daemon_bridge.py uses.
    if (const char* p = std::getenv("CHUDWARE_HOSTD_PIPE"); p && *p) return p;
    if (const char* p = std::getenv("CHUDWARE_HOSTD_SOCKET"); p && *p) return p;
    return "\\\\.\\pipe\\chudware-hostd-" + currentUserSid() + "-" + key;
}

std::string lockPath(const std::string& key) {
    // A named-mutex name, not a file path (see SingleInstance below). The
    // "Global" prefix spans sessions, so two logons of the same user still
    // share one daemon.
    return "Global\\chudware-hostd-" + currentUserSid() + "-" + key;
}

std::string guiLockPath(const std::string& key) {
    // "Local\" — NOT "Global\" like lockPath() — for two independent reasons:
    //   1. Creating a name in the global namespace needs SeCreateGlobalPrivilege, which a plain
    //      interactive user does not hold. The denial would come back as a CreateMutexW failure,
    //      indistinguishable from "someone else has it", and the app would refuse to start at all.
    //      The daemon can afford Global\ (it is a background service); a desktop window cannot.
    //   2. The guard's whole job on losing is to RAISE the incumbent's window, and a window can
    //      only be raised from inside its own interactive session — cross-session exclusion would
    //      suppress the second session's window and have nothing to show for it.
    // The SID stays for symmetry with lockPath()/socketPath() (and costs nothing).
    return "Local\\chudware-gui-" + currentUserSid() + "-" + key;
}

std::string resolveHostdBin(const std::string& hint) {
    if (const char* b = std::getenv("CHUDWARE_HOSTD_BIN"); b && *b) return b;
    if (!hint.empty()) {
        DWORD attrs = GetFileAttributesW(widen(hint).c_str());
        if (attrs != INVALID_FILE_ATTRIBUTES && !(attrs & FILE_ATTRIBUTE_DIRECTORY))
            return hint;
    }
    return "";
}

// Named-mutex single instance. Like the POSIX flock, the claim evaporates on
// process death (last handle close frees the name) — no stale state on disk.
bool SingleInstance::acquire(const std::string& lockFile, std::string& err) {
    release();
    HANDLE h = CreateMutexW(nullptr, /*initial owner*/ TRUE, widen(lockFile).c_str());
    if (!h) {
        err = "CreateMutexW " + lockFile + " " + lastErr("");
        return false;
    }
    if (GetLastError() == ERROR_ALREADY_EXISTS) {
        // Another daemon owns the name (we still got a handle; drop it).
        CloseHandle(h);
        err = "another instance holds " + lockFile;
        return false;
    }
    h_ = h;
    path_ = lockFile;
    return true;
}

void SingleInstance::release() {
    if (h_) {
        // No ReleaseMutex on purpose: the mutex EXISTENCE is the claim, and the
        // name dies with the last handle. (An abandoned-mutex state is harmless
        // here — nobody ever waits on it, they only test ERROR_ALREADY_EXISTS.)
        CloseHandle((HANDLE)h_);
        h_ = nullptr;
        path_.clear();
    }
    // Named pipes have no sock file; sock_path_ is a no-op on Windows (cleared for
    // API parity with the POSIX drain-window path).
    sock_path_.clear();
}

bool spawnDetached(const std::string& exe, const std::string& arg, std::string& err) {
    // Detach analog of the POSIX double-fork: no console, own process group, and
    // — critically — try to BREAK OUT of any Job Object the spawner sits in.
    // MCP clients under Claude Code often live in a KILL_ON_JOB_CLOSE job; a
    // daemon left inside it would die when that agent session ends, which is
    // exactly what a shared daemon must not do (plan Top-risk 1).
    std::wstring wexe = widen(exe);
    std::wstring cmd = L"\"" + wexe + L"\"";
    if (!arg.empty()) cmd += L" " + widen(arg);

    auto tryCreate = [&](DWORD flags) -> DWORD {
        STARTUPINFOW si{};
        si.cb = sizeof si;
        PROCESS_INFORMATION pi{};
        std::vector<wchar_t> cmdline(cmd.begin(), cmd.end());
        cmdline.push_back(L'\0');  // CreateProcessW may modify lpCommandLine
        if (!CreateProcessW(wexe.c_str(), cmdline.data(), nullptr, nullptr,
                            /*inherit handles*/ FALSE, flags, nullptr, nullptr, &si,
                            &pi))
            return GetLastError();
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        return ERROR_SUCCESS;
    };

    const DWORD base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP;
    DWORD e = tryCreate(base | CREATE_BREAKAWAY_FROM_JOB);
    if (e == ERROR_SUCCESS) return true;

    if (e == ERROR_ACCESS_DENIED) {
        // Likely breakaway denied by the enclosing job. Confirm, then retry
        // WITHOUT the flag — a contained daemon beats no daemon (autosave bounds
        // the loss; the next client respawns when the job dies).
        BOOL inJob = FALSE;
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION info{};
        if (IsProcessInJob(GetCurrentProcess(), nullptr, &inJob) && inJob &&
            QueryInformationJobObject(nullptr, JobObjectExtendedLimitInformation,
                                      &info, sizeof info, nullptr)) {
            const DWORD lf = info.BasicLimitInformation.LimitFlags;
            if (!(lf & JOB_OBJECT_LIMIT_BREAKAWAY_OK)) {
                if (lf & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
                    std::fprintf(
                        stderr,
                        "[chudware-hostd] WARNING: spawner is inside a KILL_ON_JOB_CLOSE "
                        "job that denies breakaway — the daemon will DIE with this "
                        "job (e.g. when the agent session ends). Start the Chudware "
                        "GUI first for a durable daemon.\n");
                e = tryCreate(base);
                if (e == ERROR_SUCCESS) return true;
            }
        }
    }
    err = "CreateProcessW " + exe + " failed (" + std::to_string(e) + ")";
    return false;
}

}  // namespace chudware_ipc

#endif  // _WIN32
