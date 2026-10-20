// CppCore implementation — see cpp_core.h.
#include <cstdio>
#include "cpp_core.h"
#include "security/integrity.h"
#include "util/platform_paths.h"

#include <filesystem>

#include <cstdlib>
#include <vector>

#if defined(__EMSCRIPTEN__)
// Browser never dlopens libchudware; keep platform helpers out of the TU.
#elif defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#else
#  include <dlfcn.h>
#  include <fcntl.h>
#  include <unistd.h>
#endif

namespace core {

namespace {

#if defined(_WIN32) && !defined(__EMSCRIPTEN__)
// UTF-8 -> wide, for path APIs.
std::wstring widen(const std::string& s) {
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), w.data(), n);
    return w;
}

std::string narrow(const std::wstring& path) {
    if (path.empty()) return {};
    const int size = WideCharToMultiByte(CP_UTF8, 0, path.data(),
        static_cast<int>(path.size()), nullptr, 0, nullptr, nullptr);
    std::string result(size, '\0');
    WideCharToMultiByte(CP_UTF8, 0, path.data(), static_cast<int>(path.size()),
                        result.data(), size, nullptr, nullptr);
    return result;
}

// Directory containing the running host exe (where libchudware.dll is staged for a
// shipped build / by build.bat).
std::wstring exe_dir() {
    wchar_t buf[MAX_PATH];
    DWORD n = GetModuleFileNameW(nullptr, buf, MAX_PATH);
    std::wstring p(buf, n);
    size_t slash = p.find_last_of(L"\\/");
    return slash == std::wstring::npos ? std::wstring() : p.substr(0, slash);
}
#elif !defined(__EMSCRIPTEN__)
// The engine's shared-library filename on this OS: Mach-O wants .dylib, ELF .so.
#  if defined(__APPLE__)
constexpr const char* kEngineLib = "libchudware.dylib";
#  else
constexpr const char* kEngineLib = "libchudware.so";
#  endif

// Directory containing the running host exe (where the engine is staged).
std::string exe_dir() {
    const std::string p = chudware::platform::executablePath();
    const size_t slash = p.find_last_of('/');
    return slash == std::string::npos ? std::string() : p.substr(0, slash);
}
#endif

}  // namespace

bool CppCore::ensureLoaded(std::string& err) {
    std::lock_guard<std::mutex> lk(mtx_);
    if (tried_) {
        if (!loaded_) err = "libchudware.dll not available (prior load failed)";
        return loaded_;
    }
    tried_ = true;

#if defined(__EMSCRIPTEN__)
    err = "WASM: in-process libchudware not available (daemon mode only)";
    return false;
#elif !defined(_WIN32)
// POSIX developer builds retain explicit/build-tree/loader candidates. Production
// accepts only the canonical library owned by the adjacent signed payload manifest.
std::vector<std::string> candidates;
#ifdef CHUDWARE_DEVELOPER_BUILD
if (const char* env = std::getenv("CHUDWARE_LIBCHUDWARE"); env && env[0])
    candidates.push_back(env);
#  ifdef CHUDWARE_DEFAULT_LIBCHUDWARE
candidates.push_back(CHUDWARE_DEFAULT_LIBCHUDWARE);
#  endif
std::string dir = exe_dir();
if (!dir.empty()) candidates.push_back(dir + "/" + kEngineLib);
candidates.push_back(kEngineLib);
#else
const std::filesystem::path self = chudware::platform::executablePath();
const std::filesystem::path root = chudware::security::installedPayloadRoot(self);
#if defined(__APPLE__)
const std::filesystem::path candidate = root / "Contents" / "Frameworks" / kEngineLib;
#elif defined(__linux__)
const std::filesystem::path candidate = root / "usr" / "lib" / kEngineLib;
#else
const std::filesystem::path candidate = self.parent_path() / kEngineLib;
#endif
std::error_code ec;
const std::string relative = std::filesystem::relative(candidate, root, ec).generic_string();
if (self.empty() || ec || relative.empty() || relative.rfind("../", 0) == 0) {
    err = "canonical libchudware path is outside the installed payload";
    return false;
}
int pinnedFd = ::open(candidate.c_str(), O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
if (pinnedFd < 0) {
    err = "canonical libchudware could not be opened without following links";
    return false;
}
const auto verified =
    chudware::security::verifyInstalledManifestFd(self, relative, "library", pinnedFd);
if (!verified.ok) {
    ::close(pinnedFd);
    err = "libchudware integrity verification failed: " + verified.detail;
    return false;
}
#if defined(__linux__)
candidates.push_back("/proc/self/fd/" + std::to_string(pinnedFd));
#else
candidates.push_back(candidate.string());
#endif
#endif

void* h = nullptr;
std::string loadDiag;
std::string winner;
for (const std::string& path : candidates) {
    h = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (h) { winner = path; break; }
    const char* de = dlerror();
    loadDiag += "[" + path + " -> " + (de ? de : "?") + "] ";
}
#ifndef CHUDWARE_DEVELOPER_BUILD
::close(pinnedFd);
#endif
if (!h) {
    err = std::string("could not load ") + kEngineLib + ": " + loadDiag;
    if (const char* lp = std::getenv("CHUDWARE_LOG_FILE"); lp && lp[0]) {
        if (FILE* f = std::fopen(lp, "a")) {
            std::fprintf(f, "[diag] cpp_core load FAILED: %s\n", loadDiag.c_str());
            std::fclose(f);
        }
    }
    std::fprintf(stderr, "[cpp_core] libchudware load FAILED: %s\n", loadDiag.c_str());
    return false;
}
call_ = reinterpret_cast<int (*)(const char*, const char*, char**)>(dlsym(h, "chudware_call"));
free_ = reinterpret_cast<void (*)(char*)>(dlsym(h, "chudware_free"));
abi_fn_ = reinterpret_cast<const char* (*)(void)>(dlsym(h, "chudware_abi_version"));
if (!call_ || !free_ || !abi_fn_) {
    dlclose(h);
    call_ = nullptr;
    free_ = nullptr;
    abi_fn_ = nullptr;
    err = std::string(kEngineLib) + " missing an expected export";
    return false;
}
dll_ = h;
abi_ = abi_fn_();
path_ = winner;
// Optional export (older engines predate it): the configure-time git-describe
// build id, surfaced by hostd as engine_build_id for staleness checks.
if (auto bid = reinterpret_cast<const char* (*)(void)>(dlsym(h, "chudware_build_id")))
    build_id_ = bid();
// Optional streaming export (older engines predate it): resolved best-effort, no-op if absent.
set_event_sink_ = reinterpret_cast<void (*)(void (*)(const char*, void*), void*)>(
    dlsym(h, "chudware_set_event_sink"));
set_doc_publish_sink_ = reinterpret_cast<
    void (*)(void (*)(const char*, const char*, const char*, void*), void*)>(
    dlsym(h, "chudware_set_doc_publish_sink"));
set_cache_refresh_sink_ = reinterpret_cast<
    void (*)(void (*)(const char*, const char*, void*), void*)>(
    dlsym(h, "chudware_set_cache_refresh_sink"));
activity_ = reinterpret_cast<int (*)(std::uint64_t*)>(dlsym(h, "chudware_activity"));
loaded_ = true;
// One-shot startup diagnostic: which lib actually serves in-proc agent runs.
std::fprintf(stderr,
             "[cpp_core] loaded libchudware abi=%s build_id=%s path=%s\n",
             abi_.c_str(),
             build_id_.empty() ? "?" : build_id_.c_str(),
             path_.c_str());
std::fflush(stderr);
return true;
#else
    std::vector<std::wstring> candidates;
#ifdef CHUDWARE_DEVELOPER_BUILD
    auto push_env = [&](const char* name) {
        if (const char* env = std::getenv(name); env && env[0]) candidates.push_back(widen(env));
    };
    push_env("CHUDWARE_LIBCHUDWARE");
    std::wstring dir = exe_dir();
    if (!dir.empty()) candidates.push_back(dir + L"\\libchudware.dll");
    if (!dir.empty()) {
        const wchar_t* rels[] = {
            L"\\..\\..\\..\\build\\windows-occt\\bin\\libchudware.dll",
            L"\\..\\..\\..\\..\\build\\windows-occt\\bin\\libchudware.dll",
            L"\\..\\..\\..\\core\\build-occt\\bin\\Release\\libchudware.dll",
            L"\\..\\..\\..\\dist\\Chudware\\libchudware.dll",
            L"\\..\\..\\..\\..\\dist\\Chudware\\libchudware.dll",
        };
        for (const wchar_t* rel : rels) candidates.push_back(dir + rel);
    }
    candidates.push_back(L"libchudware.dll");
#else
    const std::filesystem::path self = chudware::platform::executablePath();
    const std::filesystem::path root = chudware::security::installedPayloadRoot(self);
    const std::filesystem::path candidate = self.parent_path() / "libchudware.dll";
    std::error_code ec;
    const std::string relative = std::filesystem::relative(candidate, root, ec).generic_string();
    if (self.empty() || ec || relative.empty() || relative.rfind("../", 0) == 0) {
        err = "canonical libchudware path is outside the installed payload";
        return false;
    }
    HANDLE pinnedFile = INVALID_HANDLE_VALUE;
    const auto verified = [&] {
        pinnedFile = CreateFileW(candidate.c_str(), GENERIC_READ, FILE_SHARE_READ,
                                 nullptr, OPEN_EXISTING,
                                 FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
        if (pinnedFile == INVALID_HANDLE_VALUE) {
            chudware::security::FileVerdict failed;
            failed.detail = "canonical libchudware could not be opened exclusively";
            return failed;
        }
        return chudware::security::verifyInstalledManifestHandle(
            self, relative, "library", pinnedFile);
    }();
    if (!verified.ok) {
        if (pinnedFile != INVALID_HANDLE_VALUE) CloseHandle(pinnedFile);
        err = "libchudware integrity verification failed: " + verified.detail;
        return false;
    }
    candidates.push_back(candidate.wstring());
#endif
    std::string loadDiag;
    std::wstring winnerW;
    HMODULE h = nullptr;
    for (const std::wstring& path : candidates) {
#ifdef CHUDWARE_DEVELOPER_BUILD
        size_t slash = path.find_last_of(L"\\/");
        if (slash != std::wstring::npos) SetDllDirectoryW(path.substr(0, slash).c_str());
        h = LoadLibraryExW(path.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
#else
        h = LoadLibraryExW(path.c_str(), nullptr,
                           LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
#endif
        if (h) { winnerW = path; break; }
        DWORD gle = GetLastError();
        loadDiag.push_back('[');
        loadDiag += narrow(path);
        loadDiag += " -> Win32 ";
        loadDiag += std::to_string(gle);
        loadDiag += "] ";
    }
#ifndef CHUDWARE_DEVELOPER_BUILD
    CloseHandle(pinnedFile);
#endif
#ifdef CHUDWARE_DEVELOPER_BUILD
    SetDllDirectoryW(nullptr);
#endif
    if (!h) {
        err = "could not load libchudware.dll: " + loadDiag;
        if (const char* lp = std::getenv("CHUDWARE_LOG_FILE"); lp && lp[0]) {
            if (FILE* f = std::fopen(lp, "a")) {
                std::fprintf(f, "[diag] cpp_core load FAILED: %s\n", loadDiag.c_str());
                std::fclose(f);
            }
        }
        return false;
    }

    call_ = reinterpret_cast<int (*)(const char*, const char*, char**)>(
        GetProcAddress(h, "chudware_call"));
    free_ = reinterpret_cast<void (*)(char*)>(GetProcAddress(h, "chudware_free"));
    abi_fn_ = reinterpret_cast<const char* (*)(void)>(GetProcAddress(h, "chudware_abi_version"));
    if (!call_ || !free_ || !abi_fn_) {
        FreeLibrary(h);
        call_ = nullptr;
        free_ = nullptr;
        abi_fn_ = nullptr;
        err = "libchudware.dll missing an expected export";
        return false;
    }
    dll_ = h;
    abi_ = abi_fn_();
    path_ = narrow(winnerW);
    // Optional export (older engines predate it) — see the POSIX branch.
    if (auto bid = reinterpret_cast<const char* (*)(void)>(
            GetProcAddress(h, "chudware_build_id")))
        build_id_ = bid();
    // Optional streaming export (older engines predate it): resolved best-effort.
    set_event_sink_ = reinterpret_cast<void (*)(void (*)(const char*, void*), void*)>(
        GetProcAddress(h, "chudware_set_event_sink"));
    set_doc_publish_sink_ = reinterpret_cast<
        void (*)(void (*)(const char*, const char*, const char*, void*), void*)>(
        GetProcAddress(h, "chudware_set_doc_publish_sink"));
    set_cache_refresh_sink_ = reinterpret_cast<
        void (*)(void (*)(const char*, const char*, void*), void*)>(
        GetProcAddress(h, "chudware_set_cache_refresh_sink"));
    activity_ = reinterpret_cast<int (*)(std::uint64_t*)>(
        GetProcAddress(h, "chudware_activity"));
    loaded_ = true;
    return true;
#endif
}
bool CppCore::call(const std::string& method, const nlohmann::json& params,
                   nlohmann::json& result, std::string& err) {
    result = nlohmann::json::object();
    if (!ensureLoaded(err)) return false;

    std::string params_str = params.is_null() ? std::string("{}") : params.dump();
    char* out = nullptr;
    int rc = call_(method.c_str(), params_str.c_str(), &out);
    std::string out_str;
    if (out) {
        out_str = out;
        free_(out);
    }
    if (!out_str.empty()) {
        try {
            result = nlohmann::json::parse(out_str);
        } catch (...) {
            result = nlohmann::json::object();
        }
    }
    if (rc != 0) {
        err = result.contains("error") && result["error"].is_string()
                  ? result["error"].get<std::string>()
                  : ("chudware_call returned " + std::to_string(rc));
        return false;
    }
    return true;
}

void CppCore::setEventSink(void (*cb)(const char* event_json, void* ud), void* ud) {
    std::string e;
    if (!ensureLoaded(e)) return;
    if (set_event_sink_) set_event_sink_(cb, ud);
}

bool CppCore::hasEventSink() {
    std::string e;
    return ensureLoaded(e) && set_event_sink_ != nullptr;
}

void CppCore::setDocPublishSink(
    void (*cb)(const char* method, const char* params_json, const char* result_json, void* ud),
    void* ud) {
    std::string e;
    if (!ensureLoaded(e)) return;
    if (set_doc_publish_sink_) set_doc_publish_sink_(cb, ud);
}

bool CppCore::hasDocPublishSink() {
    std::string e;
    return ensureLoaded(e) && set_doc_publish_sink_ != nullptr;
}

bool CppCore::setCacheRefreshSink(
    void (*cb)(const char* doc_id, const char* response_json, void* ud), void* ud) {
    std::string err;
    if (!ensureLoaded(err) || !set_cache_refresh_sink_) return false;
    set_cache_refresh_sink_(cb, ud);
    return true;
}

bool CppCore::activity(std::uint64_t counts[3]) {
    counts[0] = counts[1] = counts[2] = 0;
    // Census must neither initiate engine loading nor manufacture a zero for an
    // older engine. hostd publishes engineState only after ensureLoaded returns.
    return loaded_ && activity_ && activity_(counts) == 0;
}


CppCore& cpp_core() {
    static CppCore instance;
    return instance;
}

}  // namespace core
