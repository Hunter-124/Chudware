// Windows implementation of daemon discovery/lifecycle (POSIX sibling:
// ipc_discovery.cpp).
//
// The function names stay transport-neutral on purpose:
//   socketPath(key) -> \\.\pipe\chudware-hostd-<SID>-<key>   (a pipe NAME, not a file)
//   lockPath(key)   -> owner-protected state-directory lock file
// so hostd/main.cpp and ipc_client.cpp compile unchanged on both platforms.
// Pipe SID names isolate transport; singleton claims use private filesystem
// state rather than a predictable public kernel-object namespace.
#ifdef _WIN32

#include "ipc_discovery.h"
#include "ipc_private.h"

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
#include <filesystem>

#include "security/integrity.h"
#include "util/platform_paths.h"

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
#ifdef CHUDWARE_DEVELOPER_BUILD
    if (const char* k = std::getenv("CHUDWARE_INSTANCE_KEY"); k && *k) return k;
#endif
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
    // Private filesystem state holds daemon and session-scoped GUI lock files.
    return stateDir();
}

std::string stateDir() {
    const char* appdata = std::getenv("APPDATA");
    if (!appdata || !*appdata) return "";
    const std::string dir = std::string(appdata) + "\\Chudware";
    return privateDir(dir) ? dir : std::string();
}

std::string socketPath(const std::string& key) {
#ifdef CHUDWARE_DEVELOPER_BUILD
    if (const char* p = std::getenv("CHUDWARE_HOSTD_PIPE"); p && *p) return p;
    if (const char* p = std::getenv("CHUDWARE_HOSTD_SOCKET"); p && *p) return p;
#endif
    return "\\\\.\\pipe\\chudware-hostd-" + currentUserSid() + "-" + key;
}

std::string lockPath(const std::string& key) {
    const std::string dir = runtimeDir();
    return dir.empty() ? "" : dir + "\\hostd-" + fnv1aHex(key) + ".lock";
}

std::string guiLockPath(const std::string& key) {
    // Preserve the GUI's per-interactive-session singleton, without a public
    // named object another OS account can pre-create.
    DWORD session = 0;
    if (!ProcessIdToSessionId(GetCurrentProcessId(), &session)) return "";
    const std::string dir = runtimeDir();
    return dir.empty() ? "" : dir + "\\gui-" + std::to_string(session) +
                                  "-" + fnv1aHex(key) + ".lock";
}

std::string resolveHostdBin(const std::string& hint) {
#ifdef CHUDWARE_DEVELOPER_BUILD
    if (const char* b = std::getenv("CHUDWARE_HOSTD_BIN"); b && *b) return b;
    if (!hint.empty()) {
        DWORD attrs = GetFileAttributesW(widen(hint).c_str());
        if (attrs != INVALID_FILE_ATTRIBUTES &&
            !(attrs & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)))
            return hint;
    }
    return "";
#else
    (void)hint;
    namespace fs = std::filesystem;
    const fs::path self = chudware::platform::executablePath();
    if (self.empty()) return "";
    const fs::path root = chudware::security::installedPayloadRoot(self);
    const fs::path candidate = self.parent_path() / "chudware-hostd.exe";
    std::error_code ec;
    const std::string relative = fs::relative(candidate, root, ec).generic_string();
    if (ec || relative.empty() || relative.rfind("../", 0) == 0) return "";
    const auto verified =
        chudware::security::verifyInstalledManifestEntry(self, relative, "worker");
    return verified.ok ? candidate.string() : std::string();
#endif
}

// Owner-protected file, opened without write/delete sharing. The live handle,
// not file existence, is the claim, and process death releases it automatically.
bool SingleInstance::acquire(const std::string& lockFile, std::string& err) {
    release();
    const auto path = std::filesystem::u8path(lockFile);
    if (lockFile.empty() || !path.has_parent_path() ||
        !privateDir(narrow(path.parent_path().native()))) {
        err = "singleton requires a private user directory";
        return false;
    }
    FILE* file = privateFile(lockFile);
    if (!file) {
        err = "cannot acquire owner-private singleton " + lockFile;
        return false;
    }
    h_ = file;
    path_ = lockFile;
    std::fprintf(file, "%lu\n", GetCurrentProcessId());
    std::fflush(file);
    return true;
}

void SingleInstance::release() {
    if (h_) {
        std::fclose(static_cast<FILE*>(h_));
        h_ = nullptr;
        path_.clear();
    }
    // Named pipes have no sock file; sock_path_ is a no-op on Windows (cleared for
    // API parity with the POSIX drain-window path).
    sock_path_.clear();
}

bool spawnDetached(const std::string& exe, const std::string& arg, std::string& err) {
    std::wstring wexe = widen(exe);
    std::wstring cmd = L"\"" + wexe + L"\"";
    if (!arg.empty()) cmd += L" " + widen(arg);
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
    HANDLE pinned = CreateFileW(wexe.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                                OPEN_EXISTING,
                                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (pinned == INVALID_HANDLE_VALUE) {
        err = lastErr("open verified hostd");
        return false;
    }
    const auto verified = chudware::security::verifyInstalledManifestHandle(
        self, relative, "worker", pinned);
    if (!verified.ok) {
        CloseHandle(pinned);
        err = "hostd integrity verification failed: " + verified.detail;
        return false;
    }
    BY_HANDLE_FILE_INFORMATION pinnedInfo{};
    if (!GetFileInformationByHandle(pinned, &pinnedInfo)) {
        CloseHandle(pinned); err = lastErr("inspect verified hostd"); return false;
    }
#endif

    auto tryCreate = [&](DWORD flags) -> DWORD {
        STARTUPINFOW si{}; si.cb = sizeof si;
        PROCESS_INFORMATION pi{};
        std::vector<wchar_t> cmdline(cmd.begin(), cmd.end()); cmdline.push_back(L'\0');
        if (!CreateProcessW(wexe.c_str(), cmdline.data(), nullptr, nullptr, FALSE,
                            flags | CREATE_SUSPENDED, nullptr, nullptr, &si, &pi))
            return GetLastError();
#ifndef CHUDWARE_DEVELOPER_BUILD
        wchar_t image[MAX_PATH]; DWORD imageLen = MAX_PATH;
        bool same = QueryFullProcessImageNameW(pi.hProcess, 0, image, &imageLen) != FALSE;
        HANDLE childImage = INVALID_HANDLE_VALUE;
        if (same) childImage = CreateFileW(image, GENERIC_READ, FILE_SHARE_READ, nullptr,
                                           OPEN_EXISTING,
                                           FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                                           nullptr);
        BY_HANDLE_FILE_INFORMATION childInfo{};
        same = same && childImage != INVALID_HANDLE_VALUE &&
               GetFileInformationByHandle(childImage, &childInfo) &&
               childInfo.dwVolumeSerialNumber == pinnedInfo.dwVolumeSerialNumber &&
               childInfo.nFileIndexHigh == pinnedInfo.nFileIndexHigh &&
               childInfo.nFileIndexLow == pinnedInfo.nFileIndexLow;
        if (childImage != INVALID_HANDLE_VALUE) CloseHandle(childImage);
        if (!same) {
            TerminateProcess(pi.hProcess, ERROR_BAD_EXE_FORMAT);
            CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
            return ERROR_BAD_EXE_FORMAT;
        }
#endif
        if (ResumeThread(pi.hThread) == static_cast<DWORD>(-1)) {
            DWORD e = GetLastError(); TerminateProcess(pi.hProcess, e);
            CloseHandle(pi.hThread); CloseHandle(pi.hProcess); return e;
        }
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
        return ERROR_SUCCESS;
    };

    const DWORD base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP;
    DWORD e = tryCreate(base | CREATE_BREAKAWAY_FROM_JOB);
    if (e == ERROR_ACCESS_DENIED) {
        BOOL inJob = FALSE; JOBOBJECT_EXTENDED_LIMIT_INFORMATION info{};
        if (IsProcessInJob(GetCurrentProcess(), nullptr, &inJob) && inJob &&
            QueryInformationJobObject(nullptr, JobObjectExtendedLimitInformation,
                                      &info, sizeof info, nullptr) &&
            !(info.BasicLimitInformation.LimitFlags & JOB_OBJECT_LIMIT_BREAKAWAY_OK))
            e = tryCreate(base);
    }
#ifndef CHUDWARE_DEVELOPER_BUILD
    CloseHandle(pinned);
#endif
    if (e == ERROR_SUCCESS) return true;
    err = "CreateProcessW " + exe + " failed (" + std::to_string(e) + ")";
    return false;
}

}  // namespace chudware_ipc

#endif  // _WIN32
