#include "bootstrap.h"

#include <windows.h>
#include <bcrypt.h>

#include <array>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <string>
#include <vector>

#pragma comment(lib, "bcrypt.lib")

namespace {
std::string narrow(const std::wstring& w) {
    if (w.empty()) return "";
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string s(n, 0);
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), s.data(), n, nullptr, nullptr);
    return s;
}
std::string env_or(const char* k, const std::string& fb) {
    char* v = nullptr;
    size_t n = 0;
    if (_dupenv_s(&v, &n, k) == 0 && v) {
        std::string s(v);
        free(v);
        return s;
    }
    return fb;
}
bool fileExists(const std::string& p) {
    DWORD a = GetFileAttributesA(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}
bool dirExists(const std::string& p) {
    DWORD a = GetFileAttributesA(p.c_str());
    return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY);
}
std::string appdataRoot() { return env_or("APPDATA", "") + "\\AI-CAD"; }

std::string runCapture(const std::string& cmd) {
    std::string out;
    FILE* p = _popen(cmd.c_str(), "r");
    if (!p) return "";
    std::array<char, 256> buf;
    while (fgets(buf.data(), (int)buf.size(), p)) out += buf.data();
    _pclose(p);
    while (!out.empty() && (out.back() == '\n' || out.back() == '\r')) out.pop_back();
    return out;
}
std::string resolveBasePython(const std::string& dir) {
    std::string embed = dir + "\\python-embed\\python.exe";
    if (fileExists(embed)) return embed;
    std::string r = runCapture("py -3.11 -c \"import sys;print(sys.executable)\"");
    if (fileExists(r)) return r;
    r = runCapture("python -c \"import sys;print(sys.executable)\"");
    if (fileExists(r)) return r;
    return "";
}
int runWait(const std::string& cmd, const std::string& cwd) {
    STARTUPINFOA si{};
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi{};
    std::vector<char> c(cmd.begin(), cmd.end());
    c.push_back(0);
    if (!CreateProcessA(nullptr, c.data(), nullptr, nullptr, FALSE, 0, nullptr,
                        cwd.empty() ? nullptr : cwd.c_str(), &si, &pi))
        return -1;
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}

// A provisionable source tree: a dir with installer/provision.py + requirements.txt, plus
// an optional offline wheelhouse beside it.
struct Payload {
    std::string srcDir;
    std::string wheelhouse;
};
bool hasSource(const std::string& d) {
    return fileExists(d + "\\installer\\provision.py") && fileExists(d + "\\requirements.txt");
}
bool findPayload(Payload& p) {
    const std::string dir = exeDir();
    // 1) installed / dist layout: app/ + wheelhouse/ shipped next to the exe.
    if (hasSource(dir + "\\app")) {
        p.srcDir = dir + "\\app";
        if (dirExists(dir + "\\wheelhouse")) p.wheelhouse = dir + "\\wheelhouse";
        return true;
    }
    // 2) explicit dev override.
    std::string repo = env_or("AI_CAD_REPO", "");
    if (!repo.empty() && hasSource(repo)) {
        p.srcDir = repo;
        if (dirExists(repo + "\\wheelhouse")) p.wheelhouse = repo + "\\wheelhouse";
        return true;
    }
    // 3) running from a dev build (host\build\bin\Release\AI-CAD.exe): walk up to the checkout.
    std::string d = dir;
    for (int i = 0; i < 6; ++i) {
        if (hasSource(d)) {
            p.srcDir = d;
            if (dirExists(d + "\\wheelhouse")) p.wheelhouse = d + "\\wheelhouse";
            return true;
        }
        size_t s = d.find_last_of("\\/");
        if (s == std::string::npos) break;
        d = d.substr(0, s);
    }
    return false;
}

std::string readFileBytes(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return "";
    return std::string((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}

// First 16 hex chars of the SHA-256 of `bytes` — matches provision.py's
// hashlib.sha256(...).hexdigest()[:16] so the C++ readiness check agrees with the Python one.
std::string sha256hex16(const std::string& bytes) {
    BCRYPT_ALG_HANDLE alg = nullptr;
    if (!BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0)))
        return "";
    unsigned char digest[32];
    NTSTATUS st = BCryptHash(alg, nullptr, 0,
                             reinterpret_cast<PUCHAR>(const_cast<char*>(bytes.data())),
                             (ULONG)bytes.size(), digest, sizeof(digest));
    BCryptCloseAlgorithmProvider(alg, 0);
    if (!BCRYPT_SUCCESS(st)) return "";
    static const char hx[] = "0123456789abcdef";
    std::string out;
    out.reserve(16);
    for (int i = 0; i < 8; ++i) {  // 8 bytes -> 16 hex chars
        out += hx[(digest[i] >> 4) & 0xF];
        out += hx[digest[i] & 0xF];
    }
    return out;
}

// Pull "req_hash" out of %APPDATA%\AI-CAD\runtime\.ready.json (written by provision.py).
std::string stampReqHash() {
    std::string s = readFileBytes(appdataRoot() + "\\runtime\\.ready.json");
    const std::string key = "\"req_hash\"";
    size_t k = s.find(key);
    if (k == std::string::npos) return "";
    size_t q1 = s.find('"', s.find(':', k + key.size()));
    if (q1 == std::string::npos) return "";
    size_t q2 = s.find('"', q1 + 1);
    if (q2 == std::string::npos) return "";
    return s.substr(q1 + 1, q2 - q1 - 1);
}

// True unless we can positively prove the installed venv is stale. The provisioning stamp's
// req_hash must equal the hash of the requirements the payload would install; a mismatch (or a
// missing stamp next to an existing python.exe) means a dependency was added/changed since the
// venv was built — exactly the case that surfaced as a cryptic "No module named ..." at runtime.
// If the payload's requirements can't be located (or hashing is unavailable) we don't block.
bool installedVenvFresh() {
    Payload pl;
    if (!findPayload(pl)) return true;
    std::string want = sha256hex16(readFileBytes(pl.srcDir + "\\requirements.txt"));
    if (want.empty()) return true;
    std::string have = stampReqHash();
    return !have.empty() && have == want;
}

bool provisionRuntime(bool force, std::string& log) {
    if (!force && runtimeReady()) {
        log = "runtime already present";
        return true;
    }
    Payload pl;
    if (!findPayload(pl)) {
        log = "runtime payload not found - run the packaged build (dist\\AI-CAD\\AI-CAD.exe) or "
              "install.ps1, or set AI_CAD_REPO to your source checkout.";
        return false;
    }
    std::string base = resolveBasePython(exeDir());
    if (base.empty()) {
        log = "no base Python 3.11 found - install Python 3.11 (or bundle python-embed/) to build "
              "the runtime.";
        return false;
    }
    std::string cmd = "\"" + base + "\" -m installer.provision --base-python \"" + base +
                      "\" --with-app-code";
    if (force) cmd += " --force";
    if (!pl.wheelhouse.empty()) cmd += " --wheelhouse \"" + pl.wheelhouse + "\"";
    int rc = runWait(cmd, pl.srcDir);
    log = std::string(force ? "repaired runtime" : "provisioned runtime") +
          " (rc=" + std::to_string(rc) + ", base=" + base + ")";
    return rc == 0 && runtimeReady();
}
}  // namespace

std::string exeDir() {
    wchar_t buf[MAX_PATH];
    DWORD n = GetModuleFileNameW(nullptr, buf, MAX_PATH);
    std::wstring w(buf, n);
    size_t s = w.find_last_of(L"\\/");
    if (s != std::wstring::npos) w = w.substr(0, s);
    return narrow(w);
}

std::string runtimePython() {
    std::string envp = env_or("AI_CAD_PYTHON", "");
    if (!envp.empty() && fileExists(envp)) return envp;
    std::string installed = appdataRoot() + "\\runtime\\venv\\Scripts\\python.exe";
    if (fileExists(installed)) return installed;
    std::string portable = exeDir() + "\\runtime\\venv\\Scripts\\python.exe";
    if (fileExists(portable)) return portable;
    return installed;  // canonical default for messaging when nothing is installed yet
}

std::string appDir() {
    // Resolve the dir holding engine/agent/mcp_server that the worker runs from. Prefer the
    // FRESHEST copy so shipped app-code fixes take effect without re-provisioning: explicit
    // dev override -> payload shipped next to the exe -> a source checkout above the exe ->
    // the installed %APPDATA% copy (last resort).
    std::string repo = env_or("AI_CAD_REPO", "");
    if (!repo.empty() && dirExists(repo + "\\engine")) return repo;
    std::string portable = exeDir() + "\\app";
    if (dirExists(portable + "\\engine") && dirExists(portable + "\\agent")) return portable;
    std::string d = exeDir();
    for (int i = 0; i < 6; ++i) {
        if (dirExists(d + "\\engine") && dirExists(d + "\\agent") &&
            fileExists(d + "\\requirements.txt"))
            return d;
        size_t s = d.find_last_of("\\/");
        if (s == std::string::npos) break;
        d = d.substr(0, s);
    }
    return appdataRoot() + "\\app";
}

bool runtimeReady() {
    std::string py = runtimePython();
    if (!fileExists(py)) return false;
    // An explicit dev python (AI_CAD_PYTHON) or a portable runtime shipped next to the exe is
    // trusted as-is. Only the managed %APPDATA% venv gets the stricter freshness check, so a
    // stale venv (its python.exe exists but its packages predate a requirements change) is
    // re-provisioned instead of being run against — which previously crashed mid-job with
    // "No module named ...".
    std::string envp = env_or("AI_CAD_PYTHON", "");
    if (!envp.empty() && fileExists(envp)) return true;
    if (py != appdataRoot() + "\\runtime\\venv\\Scripts\\python.exe") return true;
    return installedVenvFresh();
}

bool ensureRuntime(std::string& log) { return provisionRuntime(false, log); }

bool repairRuntime(std::string& log) { return provisionRuntime(true, log); }
