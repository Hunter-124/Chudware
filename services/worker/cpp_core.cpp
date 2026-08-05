// CppCore implementation — see cpp_core.h.
#include <cstdio>
#include "cpp_core.h"

#include <cstdlib>
#include <vector>

#if defined(__EMSCRIPTEN__)
// Browser never dlopens libchudware; keep platform helpers out of the TU.
#elif defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#else
#  include <dlfcn.h>
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
// Directory containing the running host exe (where libchudware.so is staged).
std::string exe_dir() {
    char buf[4096];
    ssize_t n = readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n <= 0) return std::string();
    buf[n] = '\0';
    std::string p(buf);
    size_t slash = p.find_last_of('/');
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
// POSIX: dlopen libchudware.so.
// Resolve order (first successful load wins — keep this deterministic so a
// staged dist/exe-dir binary cannot silently lose to a stale LD_LIBRARY_PATH):
//   1. $CHUDWARE_LIBCHUDWARE (or $CHUDWARE_LIBCHUDWARE) — explicit absolute path
//   2. CHUDWARE_DEFAULT_LIBCHUDWARE (compile-time build-tree path, if set by CMake)
//   3. libchudware.so next to the running host/hostd executable (staged install)
//   4. bare "libchudware.so" via the dynamic loader (rpath / LD_LIBRARY_PATH)
// Note: when the GUI is in daemon mode (default), hostd is the process that
// actually dlopens; the GUI only sees hostd's engine_build_id via hello.
// A long-lived hostd holding a stale .so is the "builds go elsewhere" trap —
// hostd self-drains on mtime change of selfBinary_ + loadedPath().
std::vector<std::string> candidates;
auto push_env = [&](const char* name) {
    if (const char* env = std::getenv(name); env && env[0])
        candidates.push_back(env);
};
push_env("CHUDWARE_LIBCHUDWARE");
push_env("CHUDWARE_LIBCHUDWARE");
#ifdef CHUDWARE_DEFAULT_LIBCHUDWARE
candidates.push_back(CHUDWARE_DEFAULT_LIBCHUDWARE);  // build-tree fallback (set by CMake)
#endif
std::string dir = exe_dir();
if (!dir.empty()) candidates.push_back(dir + "/libchudware.so");
candidates.push_back("libchudware.so");  // last resort: loader search

void* h = nullptr;
std::string loadDiag;
std::string winner;
for (const std::string& path : candidates) {
    h = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
    if (h) { winner = path; break; }
    const char* de = dlerror();
    loadDiag += "[" + path + " -> " + (de ? de : "?") + "] ";
}
if (!h) {
    err = "could not load libchudware.so: " + loadDiag;
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
    err = "libchudware.so missing an expected export";
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
// Optional sidecar-shutdown export (older engines predate it): resolved best-effort, no-op if
// absent — hostd's clean-shutdown tail tolerates missing sidecars support entirely.
shutdown_sidecars_ = reinterpret_cast<void (*)(void)>(dlsym(h, "chudware_agent_sidecars_shutdown"));
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
    // Candidate paths in priority order (mirrors MCP core_bridge + Linux build-tree fallbacks).
    std::vector<std::wstring> candidates;
    auto push_env = [&](const char* name) {
        if (const char* env = std::getenv(name); env && env[0])
            candidates.push_back(widen(env));
    };
    push_env("CHUDWARE_LIBCHUDWARE");
    push_env("CHUDWARE_LIBCHUDWARE");
    std::wstring dir = exe_dir();
    if (!dir.empty()) candidates.push_back(dir + L"\\libchudware.dll");
    // Dev-tree fallbacks: Ninja windows-occt preset, then VS multi-config OCCT build.
    // Walk up from the exe (…/host/build-win/bin → repo root) when present.
    if (!dir.empty()) {
        // host/build-win/bin → ../../../build/windows-occt/bin
        // host/build/bin/Release → ../../../../build/windows-occt/bin (rare)
        const wchar_t* rels[] = {
            L"\\..\\..\\..\\build\\windows-occt\\bin\\libchudware.dll",
            L"\\..\\..\\..\\..\\build\\windows-occt\\bin\\libchudware.dll",
            L"\\..\\..\\..\\core\\build-occt\\bin\\Release\\libchudware.dll",
            L"\\..\\..\\..\\dist\\Chudware\\libchudware.dll",
            L"\\..\\..\\..\\..\\dist\\Chudware\\libchudware.dll",
        };
        for (const wchar_t* rel : rels)
            candidates.push_back(dir + rel);
    }
    candidates.push_back(L"libchudware.dll");  // last resort: default search order

    HMODULE h = nullptr;
    std::string loadDiag;
    std::wstring winnerW;
    for (const std::wstring& path : candidates) {
        // Resolve the DLL's directory and add it to the PROCESS search path. This is the
        // crucial bit: LOAD_WITH_ALTERED_SEARCH_PATH adds that directory only for
        // libchudware's DIRECT imports — it does NOT cover TRANSITIVE deps (a dep loaded by
        // an OCCT TK*.dll or gmsh-4.15.dll). Those transitive loads use the normal process
        // search, so an OCCT-internal sibling that lives only in the build dir came back
        // ERROR_MOD_NOT_FOUND (126) inside the host, even though libchudware itself was found.
        // SetDllDirectoryW puts the build dir on the process search path, which DOES apply
        // to the whole dependency chain.
        size_t slash = path.find_last_of(L"\\/");
        if (slash != std::wstring::npos) SetDllDirectoryW(path.substr(0, slash).c_str());
        h = LoadLibraryExW(path.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
        if (h) { winnerW = path; break; }
        DWORD gle = GetLastError();
        // narrow the wide path for the diagnostic
        std::string p(path.begin(), path.end());
        loadDiag += "[" + p + " -> Win32 " + std::to_string(gle) + "] ";
    }
    SetDllDirectoryW(nullptr);  // restore the default search path
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
    {
        int n = WideCharToMultiByte(CP_UTF8, 0, winnerW.c_str(), (int)winnerW.size(),
                                    nullptr, 0, nullptr, nullptr);
        std::string p(n, '\0');
        WideCharToMultiByte(CP_UTF8, 0, winnerW.c_str(), (int)winnerW.size(), p.data(), n,
                            nullptr, nullptr);
        path_ = p;
    }
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
    // Optional sidecar-shutdown export (older engines predate it): resolved best-effort.
    shutdown_sidecars_ = reinterpret_cast<void (*)(void)>(
        GetProcAddress(h, "chudware_agent_sidecars_shutdown"));
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

void CppCore::shutdownSidecars() {
    // Deliberately does NOT call ensureLoaded() — never lazily loads the engine just to shut
    // sidecars down. Callers (hostd's clean-shutdown tail) already gate on available().
    if (loaded_ && shutdown_sidecars_) shutdown_sidecars_();
}

CppCore& cpp_core() {
    static CppCore instance;
    return instance;
}

}  // namespace core
