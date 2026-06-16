// CppCore implementation — see cpp_core.h.
#include "cpp_core.h"

#include <cstdlib>
#include <vector>

#if defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
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
    err = "CppCore only implemented on Windows";
    return false;
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
    for (const std::wstring& path : candidates) {
        // LOAD_WITH_ALTERED_SEARCH_PATH: when given an absolute path, the DLL's own
        // directory joins the search so its OCCT sibling DLLs (TK*, jemalloc, tbb12)
        // resolve without staging them next to the host exe.
        h = LoadLibraryExW(path.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH);
        if (h) break;
    }
    if (!h) {
        err = "could not load libaicad.dll (set AI_CAD_LIBAICAD or stage it next to the exe)";
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
