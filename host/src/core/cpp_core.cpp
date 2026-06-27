// CppCore implementation — see cpp_core.h.
#include <cstdio>
#include "cpp_core.h"

#include <cstdlib>
#include <vector>

#if defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#else
#  include <dlfcn.h>
#  include <unistd.h>
#endif

namespace core {

namespace {

#if defined(_WIN32)
// UTF-8 -> wide, for path APIs.
std::wstring widen(const std::string& s) {
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), w.data(), n);
    return w;
}

// Directory containing the running host exe (where libaicad.dll is staged for a
// shipped build / by build.bat).
std::wstring exe_dir() {
    wchar_t buf[MAX_PATH];
    DWORD n = GetModuleFileNameW(nullptr, buf, MAX_PATH);
    std::wstring p(buf, n);
    size_t slash = p.find_last_of(L"\\/");
    return slash == std::wstring::npos ? std::wstring() : p.substr(0, slash);
}
#else
// Directory containing the running host exe (where libaicad.so is staged).
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
        if (!loaded_) err = "libaicad.dll not available (prior load failed)";
        return loaded_;
    }
    tried_ = true;

#if !defined(_WIN32)
    // POSIX: dlopen libaicad.so. Candidate paths in priority order.
    std::vector<std::string> candidates;
    if (const char* env = std::getenv("AI_CAD_LIBAICAD"); env && env[0])
        candidates.push_back(env);
#ifdef AICAD_DEFAULT_LIBAICAD
    candidates.push_back(AICAD_DEFAULT_LIBAICAD);  // build-tree fallback (set by CMake)
#endif
    std::string dir = exe_dir();
    if (!dir.empty()) candidates.push_back(dir + "/libaicad.so");
    candidates.push_back("libaicad.so");  // last resort: loader search (rpath / LD_LIBRARY_PATH)

    void* h = nullptr;
    std::string loadDiag;
    for (const std::string& path : candidates) {
        h = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL);
        if (h) break;
        const char* de = dlerror();
        loadDiag += "[" + path + " -> " + (de ? de : "?") + "] ";
    }
    if (!h) {
        err = "could not load libaicad.so: " + loadDiag;
        if (const char* lp = std::getenv("AI_CAD_LOG_FILE"); lp && lp[0]) {
            if (FILE* f = std::fopen(lp, "a")) {
                std::fprintf(f, "[diag] cpp_core load FAILED: %s\n", loadDiag.c_str());
                std::fclose(f);
            }
        }
        return false;
    }
    call_ = reinterpret_cast<int (*)(const char*, const char*, char**)>(dlsym(h, "aicad_call"));
    free_ = reinterpret_cast<void (*)(char*)>(dlsym(h, "aicad_free"));
    abi_fn_ = reinterpret_cast<const char* (*)(void)>(dlsym(h, "aicad_abi_version"));
    if (!call_ || !free_ || !abi_fn_) {
        dlclose(h);
        call_ = nullptr;
        free_ = nullptr;
        abi_fn_ = nullptr;
        err = "libaicad.so missing an expected export";
        return false;
    }
    dll_ = h;
    abi_ = abi_fn_();
    loaded_ = true;
    return true;
#else
    // Candidate paths in priority order.
    std::vector<std::wstring> candidates;
    if (const char* env = std::getenv("AI_CAD_LIBAICAD"); env && env[0]) {
        candidates.push_back(widen(env));
    }
    std::wstring dir = exe_dir();
    if (!dir.empty()) candidates.push_back(dir + L"\\libaicad.dll");
    candidates.push_back(L"libaicad.dll");  // last resort: default search order

    HMODULE h = nullptr;
    std::string loadDiag;
    for (const std::wstring& path : candidates) {
        // Resolve the DLL's directory and add it to the PROCESS search path. This is the
        // crucial bit: LOAD_WITH_ALTERED_SEARCH_PATH adds that directory only for
        // libaicad's DIRECT imports — it does NOT cover TRANSITIVE deps (a dep loaded by
        // an OCCT TK*.dll or gmsh-4.15.dll). Those transitive loads use the normal process
        // search, so an OCCT-internal sibling that lives only in the build dir came back
        // ERROR_MOD_NOT_FOUND (126) inside the host, even though libaicad itself was found.
        // SetDllDirectoryW puts the build dir on the process search path, which DOES apply
        // to the whole dependency chain.
        size_t slash = path.find_last_of(L"\\/");
        if (slash != std::wstring::npos) SetDllDirectoryW(path.substr(0, slash).c_str());
        h = LoadLibraryExW(path.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
        if (h) break;
        DWORD gle = GetLastError();
        // narrow the wide path for the diagnostic
        std::string p(path.begin(), path.end());
        loadDiag += "[" + p + " -> Win32 " + std::to_string(gle) + "] ";
    }
    SetDllDirectoryW(nullptr);  // restore the default search path
    if (!h) {
        err = "could not load libaicad.dll: " + loadDiag;
        if (const char* lp = std::getenv("AI_CAD_LOG_FILE"); lp && lp[0]) {
            if (FILE* f = std::fopen(lp, "a")) {
                std::fprintf(f, "[diag] cpp_core load FAILED: %s\n", loadDiag.c_str());
                std::fclose(f);
            }
        }
        return false;
    }

    call_ = reinterpret_cast<int (*)(const char*, const char*, char**)>(
        GetProcAddress(h, "aicad_call"));
    free_ = reinterpret_cast<void (*)(char*)>(GetProcAddress(h, "aicad_free"));
    abi_fn_ = reinterpret_cast<const char* (*)(void)>(GetProcAddress(h, "aicad_abi_version"));
    if (!call_ || !free_ || !abi_fn_) {
        FreeLibrary(h);
        call_ = nullptr;
        free_ = nullptr;
        abi_fn_ = nullptr;
        err = "libaicad.dll missing an expected export";
        return false;
    }
    dll_ = h;
    abi_ = abi_fn_();
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
                  : ("aicad_call returned " + std::to_string(rc));
        return false;
    }
    return true;
}

CppCore& cpp_core() {
    static CppCore instance;
    return instance;
}

}  // namespace core
