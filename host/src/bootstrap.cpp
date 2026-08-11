#include "bootstrap.h"

#ifdef _WIN32

#include <windows.h>
#include <bcrypt.h>

#include <array>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <set>
#include <string>
#include <vector>

#pragma comment(lib, "bcrypt.lib")

namespace {
std::string narrow(const std::wstring &w) {
  if (w.empty())
    return "";
  int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0,
                              nullptr, nullptr);
  std::string s(n, 0);
  WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), s.data(), n,
                      nullptr, nullptr);
  return s;
}
std::string env_or(const char *k, const std::string &fb) {
  char *v = nullptr;
  size_t n = 0;
  if (_dupenv_s(&v, &n, k) == 0 && v) {
    std::string s(v);
    free(v);
    return s;
  }
  return fb;
}
bool fileExists(const std::string &p) {
  DWORD a = GetFileAttributesA(p.c_str());
  return a != INVALID_FILE_ATTRIBUTES && !(a & FILE_ATTRIBUTE_DIRECTORY);
}
bool dirExists(const std::string &p) {
  DWORD a = GetFileAttributesA(p.c_str());
  return a != INVALID_FILE_ATTRIBUTES && (a & FILE_ATTRIBUTE_DIRECTORY);
}
// Pre-rename data-root names, newest first. MUST stay in sync with
// services/paths.py APP_NAMES_LEGACY — that module is the reference implementation
// of this rule and the Python worker resolves the same root independently.
const char *const kLegacyAppNames[] = {"Billet", "AI-CAD"};

// Copy-if-missing tree merge for the one-time legacy migration. Best-effort:
// false when anything couldn't be copied (caller keeps the legacy root this boot).
bool copyTreeIfMissing(const std::filesystem::path &src,
                       const std::filesystem::path &dst) {
  namespace fs = std::filesystem;
  std::error_code ec;
  if (fs::is_regular_file(src, ec)) {
    if (fs::exists(dst, ec))
      return true;
    fs::create_directories(dst.parent_path(), ec);
    ec.clear();
    fs::copy_file(src, dst, ec);
    if (ec && fs::exists(dst, ec))
      ec.clear(); // benign race with a concurrent copy
    return !ec;
  }
  fs::create_directories(dst, ec);
  bool ok = !ec;
  for (const auto &de : fs::directory_iterator(src, ec))
    ok = copyTreeIfMissing(de.path(), dst / de.path().filename()) && ok;
  if (ec)
    ok = false;
  return ok;
}

std::string appdataRoot() {
  // Chudware is the unconditional data root, mirroring services/paths.py
  // active_root(): a pre-rename install (marker: config.json) is copied forward
  // ONCE — copy-if-missing, skipping venvs / re-provisionable payloads; the old
  // dir is never moved or deleted. Only a failed copy keeps the legacy root in
  // use. Fresh installs get Chudware.
  const std::string base = env_or("APPDATA", "");
  const std::string neu = base + "\\Chudware";
  if (fileExists(neu + "\\config.json"))
    return neu;
  namespace fs = std::filesystem;
  static const std::set<std::string> kSkip = {
      "runtime",   "mcp-venv", "torch-venv",     "models",   "llamacpp",
      "cache",     "prof",     "cef_cache",      "cef_cache_daemon",
      "cef_cache_gui",
  };
  for (const char *name : kLegacyAppNames) {
    const fs::path old = fs::path(base) / name;
    std::error_code ec;
    if (!fs::exists(old / "config.json", ec))
      continue;
    bool ok = true;
    for (const auto &de : fs::directory_iterator(old, ec)) {
      if (kSkip.count(de.path().filename().string()))
        continue;
      ok = copyTreeIfMissing(de.path(), fs::path(neu) / de.path().filename()) && ok;
    }
    if (ec)
      ok = false;
    return ok ? neu : old.string();
  }
  return neu;
}

std::string runCapture(const std::string &cmd) {
  std::string out;
  FILE *p = _popen(cmd.c_str(), "r");
  if (!p)
    return "";
  std::array<char, 256> buf;
  while (fgets(buf.data(), (int)buf.size(), p))
    out += buf.data();
  _pclose(p);
  while (!out.empty() && (out.back() == '\n' || out.back() == '\r'))
    out.pop_back();
  return out;
}
std::string resolveBasePython(const std::string &dir) {
  std::string embed = dir + "\\python-embed\\python.exe";
  if (fileExists(embed))
    return embed;
  std::string r =
      runCapture("py -3.11 -c \"import sys;print(sys.executable)\"");
  if (fileExists(r))
    return r;
  r = runCapture("python -c \"import sys;print(sys.executable)\"");
  if (fileExists(r))
    return r;
  return "";
}
int runWait(const std::string &cmd, const std::string &cwd) {
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

// A provisionable source tree: a dir with services/installer/provision.py +
// requirements.txt, plus an optional offline wheelhouse beside it.
struct Payload {
  std::string srcDir;
  std::string wheelhouse;
};
bool hasSource(const std::string &d) {
  return fileExists(d + "\\installer\\provision.py") &&
         fileExists(d + "\\requirements.txt");
}
bool findPayload(Payload &p) {
  const std::string dir = exeDir();
  // 1) installed / dist layout: app/ + wheelhouse/ shipped next to the exe.
  if (hasSource(dir + "\\app")) {
    p.srcDir = dir + "\\app";
    if (dirExists(dir + "\\wheelhouse"))
      p.wheelhouse = dir + "\\wheelhouse";
    return true;
  }
  // 2) explicit dev override.
  std::string repo = env_or("CHUDWARE_REPO", "");
  if (!repo.empty() && hasSource(repo)) {
    p.srcDir = repo;
    if (dirExists(repo + "\\wheelhouse"))
      p.wheelhouse = repo + "\\wheelhouse";
    return true;
  }
  // 3) running from a dev build (host\build\bin\Release\Chudware.exe): walk up to
  // the checkout.
  std::string d = dir;
  for (int i = 0; i < 6; ++i) {
    if (hasSource(d)) {
      p.srcDir = d;
      if (dirExists(d + "\\wheelhouse"))
        p.wheelhouse = d + "\\wheelhouse";
      return true;
    }
    size_t s = d.find_last_of("\\/");
    if (s == std::string::npos)
      break;
    d = d.substr(0, s);
  }
  return false;
}

std::string readFileBytes(const std::string &path) {
  std::ifstream f(path, std::ios::binary);
  if (!f)
    return "";
  return std::string((std::istreambuf_iterator<char>(f)),
                     std::istreambuf_iterator<char>());
}

// First 16 hex chars of the SHA-256 of `bytes` — matches provision.py's
// hashlib.sha256(...).hexdigest()[:16] so the C++ readiness check agrees with
// the Python one.
std::string sha256hex16(const std::string &bytes) {
  BCRYPT_ALG_HANDLE alg = nullptr;
  if (!BCRYPT_SUCCESS(BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM,
                                                  nullptr, 0)))
    return "";
  unsigned char digest[32];
  NTSTATUS st =
      BCryptHash(alg, nullptr, 0,
                 reinterpret_cast<PUCHAR>(const_cast<char *>(bytes.data())),
                 (ULONG)bytes.size(), digest, sizeof(digest));
  BCryptCloseAlgorithmProvider(alg, 0);
  if (!BCRYPT_SUCCESS(st))
    return "";
  static const char hx[] = "0123456789abcdef";
  std::string out;
  out.reserve(16);
  for (int i = 0; i < 8; ++i) { // 8 bytes -> 16 hex chars
    out += hx[(digest[i] >> 4) & 0xF];
    out += hx[digest[i] & 0xF];
  }
  return out;
}

// Pull "req_hash" out of %APPDATA%\Chudware\runtime\.ready.json (written by
// provision.py).
std::string stampReqHash() {
  std::string s = readFileBytes(appdataRoot() + "\\runtime\\.ready.json");
  const std::string key = "\"req_hash\"";
  size_t k = s.find(key);
  if (k == std::string::npos)
    return "";
  size_t q1 = s.find('"', s.find(':', k + key.size()));
  if (q1 == std::string::npos)
    return "";
  size_t q2 = s.find('"', q1 + 1);
  if (q2 == std::string::npos)
    return "";
  return s.substr(q1 + 1, q2 - q1 - 1);
}

// True unless we can positively prove the installed venv is stale. The
// provisioning stamp's req_hash must equal the hash of the requirements the
// payload would install; a mismatch (or a missing stamp next to an existing
// python.exe) means a dependency was added/changed since the venv was built —
// exactly the case that surfaced as a cryptic "No module named ..." at runtime.
// If the payload's requirements can't be located (or hashing is unavailable) we
// don't block.
bool installedVenvFresh() {
  Payload pl;
  if (!findPayload(pl))
    return true;
  std::string want =
      sha256hex16(readFileBytes(pl.srcDir + "\\requirements.txt"));
  if (want.empty())
    return true;
  std::string have = stampReqHash();
  return !have.empty() && have == want;
}

bool provisionRuntime(bool force, std::string &log) {
  if (!force && runtimeReady()) {
    log = "runtime already present";
    return true;
  }
  Payload pl;
  if (!findPayload(pl)) {
    log = "runtime payload not found - run the packaged build "
          "(dist\\Chudware\\Chudware.exe) or "
          "install.ps1, or set CHUDWARE_REPO to your source checkout.";
    return false;
  }
  std::string base = resolveBasePython(exeDir());
  if (base.empty()) {
    log = "no base Python 3.11 found - install Python 3.11 (or bundle "
          "python-embed/) to build "
          "the runtime.";
    return false;
  }
  std::string cmd = "\"" + base + "\" -m services.installer.provision --base-python \"" +
                    base + "\" --with-app-code";
  if (force)
    cmd += " --force";
  if (!pl.wheelhouse.empty())
    cmd += " --wheelhouse \"" + pl.wheelhouse + "\"";
  int rc = runWait(cmd, pl.srcDir);
  log = std::string(force ? "repaired runtime" : "provisioned runtime") +
        " (rc=" + std::to_string(rc) + ", base=" + base + ")";
  return rc == 0 && runtimeReady();
}
} // namespace

std::string exeDir() {
  wchar_t buf[MAX_PATH];
  DWORD n = GetModuleFileNameW(nullptr, buf, MAX_PATH);
  std::wstring w(buf, n);
  size_t s = w.find_last_of(L"\\/");
  if (s != std::wstring::npos)
    w = w.substr(0, s);
  return narrow(w);
}

std::string runtimePython() {
  std::string envp = env_or("CHUDWARE_PYTHON", "");
  if (!envp.empty() && fileExists(envp))
    return envp;
  std::string installed =
      appdataRoot() + "\\runtime\\venv\\Scripts\\python.exe";
  if (fileExists(installed))
    return installed;
  std::string portable = exeDir() + "\\runtime\\venv\\Scripts\\python.exe";
  if (fileExists(portable))
    return portable;
  return installed; // canonical default for messaging when nothing is installed
                    // yet
}

// Same rule as the POSIX branch: a dir only qualifies as the worker cwd if
// `python -m services.worker_main` resolves from it (post-cutover checkouts
// have no top-level engine/). Keep the legacy engine+agent probe for staged
// payloads and old checkouts.
static bool looksLikeAppDir(const std::string &d) {
  if (fileExists(d + "\\services\\worker_main.py"))
    return true;
  return dirExists(d + "\\engine") && dirExists(d + "\\agent") &&
         fileExists(d + "\\requirements.txt");
}

std::string appDir() {
  // Resolve the dir holding engine/agent/services.mcp that the worker runs from.
  // Prefer the FRESHEST copy so shipped app-code fixes take effect without
  // re-provisioning: explicit dev override -> payload shipped next to the exe
  // -> a source checkout above the exe -> the installed %APPDATA% copy (last
  // resort).
  std::string repo = env_or("CHUDWARE_REPO", "");
  if (!repo.empty() && looksLikeAppDir(repo))
    return repo;
  std::string portable = exeDir() + "\\app";
  if (dirExists(portable + "\\engine") && dirExists(portable + "\\agent"))
    return portable;
  std::string d = exeDir();
  for (int i = 0; i < 6; ++i) {
    if (looksLikeAppDir(d))
      return d;
    size_t s = d.find_last_of("\\/");
    if (s == std::string::npos)
      break;
    d = d.substr(0, s);
  }
  return appdataRoot() + "\\app";
}

bool runtimeReady() {
  std::string py = runtimePython();
  if (!fileExists(py))
    return false;
  // An explicit dev python (CHUDWARE_PYTHON) or a portable runtime shipped next
  // to the exe is trusted as-is. Only the managed %APPDATA% venv gets the
  // stricter freshness check, so a stale venv (its python.exe exists but its
  // packages predate a requirements change) is re-provisioned instead of being
  // run against — which previously crashed mid-job with "No module named ...".
  std::string envp = env_or("CHUDWARE_PYTHON", "");
  if (!envp.empty() && fileExists(envp))
    return true;
  if (py != appdataRoot() + "\\runtime\\venv\\Scripts\\python.exe")
    return true;
  return installedVenvFresh();
}

// Point the C++ core (libchudware) at the app's bundled agent prompts. The core
// reads CHUDWARE_PROMPTS_DIR for its role prompts + cheatsheets; if it is unset
// the core falls back to "services/agent/prompts" RELATIVE TO THE PROCESS CWD. When the
// app is launched from its install dir (not the source tree) that relative path
// does not exist, so agent_run throws "role prompt unreadable/empty:
// agent\prompts\decomposer.md" and the host silently falls back to the Python
// worker -> the in-app AI fails with "unknown method: agent_run_v2". appDir()
// already resolves the dev-repo vs shipped-payload layouts, so its
// services/agent/prompts is the right, CWD-independent location. Set it via BOTH
// _putenv_s (host CRT) and the process block (so libchudware's own CRT,
// initialised when the DLL loads later, also sees it).
void ensurePromptsDir() {
  if (!env_or("CHUDWARE_PROMPTS_DIR", "").empty())
    return; // respect an explicit override
  std::string p = appDir() + "\\agent\\prompts";
  if (!dirExists(p))
    return;
  _putenv_s("CHUDWARE_PROMPTS_DIR", p.c_str());
  SetEnvironmentVariableA("CHUDWARE_PROMPTS_DIR", p.c_str());
}

bool ensureRuntime(std::string &log) {
  ensurePromptsDir();
  return provisionRuntime(false, log);
}

bool repairRuntime(std::string &log) { return provisionRuntime(true, log); }

#else // ---------------------------------------------------------------------
      // POSIX (Linux/macOS): XDG data dir, platform::executablePath(),
      // python3.11 + venv/bin.

#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <set>
#include <string>

#include "util/platform_paths.h"

namespace fs = std::filesystem;

namespace {

std::string env_or(const char *k, const std::string &fb) {
  const char *v = std::getenv(k);
  return (v && *v) ? std::string(v) : fb;
}
bool fileExists(const std::string &p) {
  std::error_code ec;
  return fs::is_regular_file(p, ec);
}
bool dirExists(const std::string &p) {
  std::error_code ec;
  return fs::is_directory(p, ec);
}
// Single-quote an argument for the shell (escaping embedded single quotes).
std::string shq(const std::string &s) {
  std::string o = "'";
  for (char c : s)
    o += (c == '\'') ? std::string("'\\''") : std::string(1, c);
  o += "'";
  return o;
}
// Pre-rename data-root names, newest first. MUST stay in sync with
// services/paths.py APP_NAMES_LEGACY — that module is the reference implementation
// of this rule and the Python worker resolves the same root independently.
const char *const kLegacyAppNames[] = {"Billet", "AI-CAD"};

// Copy-if-missing tree merge for the one-time legacy migration. Best-effort:
// false when anything couldn't be copied (caller keeps the legacy root this boot).
bool copyTreeIfMissing(const fs::path &src, const fs::path &dst) {
  std::error_code ec;
  if (fs::is_regular_file(src, ec)) {
    if (fs::exists(dst, ec))
      return true;
    fs::create_directories(dst.parent_path(), ec);
    ec.clear();
    fs::copy_file(src, dst, ec);
    if (ec && fs::exists(dst, ec))
      ec.clear(); // benign race with a concurrent copy
    return !ec;
  }
  fs::create_directories(dst, ec);
  bool ok = !ec;
  for (const auto &de : fs::directory_iterator(src, ec))
    ok = copyTreeIfMissing(de.path(), dst / de.path().filename()) && ok;
  if (ec)
    ok = false;
  return ok;
}

// XDG data root for the managed runtime/cache (mirrors %APPDATA% on Windows).
// Chudware is the unconditional data root, mirroring services/paths.py
// active_root(): a pre-rename install (marker: config.json) is copied forward
// ONCE — copy-if-missing, skipping venvs / re-provisionable payloads; the old dir
// is never moved or deleted. Only a failed copy keeps the legacy root in use.
// Fresh installs get Chudware.
std::string appdataRoot() {
  fs::path base;
  std::string xdg = env_or("XDG_DATA_HOME", "");
  std::string home = env_or("HOME", "");
  if (!xdg.empty())
    base = fs::path(xdg);
  else if (!home.empty())
    base = fs::path(home) / ".local" / "share";
  else
    base = fs::path(".");
  const fs::path neu = base / "Chudware";
  std::error_code ec;
  if (fs::exists(neu / "config.json", ec))
    return neu.string();
  static const std::set<std::string> kSkip = {
      "runtime",   "mcp-venv", "torch-venv",     "models",   "llamacpp",
      "cache",     "prof",     "cef_cache",      "cef_cache_daemon",
      "cef_cache_gui",
  };
  for (const char *name : kLegacyAppNames) {
    const fs::path old = base / name;
    if (!fs::exists(old / "config.json", ec))
      continue;
    bool ok = true;
    for (const auto &de : fs::directory_iterator(old, ec)) {
      if (kSkip.count(de.path().filename().string()))
        continue;
      ok = copyTreeIfMissing(de.path(), neu / de.path().filename()) && ok;
    }
    if (ec)
      ok = false;
    return ok ? neu.string() : old.string();
  }
  return neu.string();
}
std::string runCapture(const std::string &cmd) {
  std::string out;
  FILE *p = popen(cmd.c_str(), "r");
  if (!p)
    return "";
  char buf[256];
  size_t n;
  while ((n = fread(buf, 1, sizeof(buf), p)) > 0)
    out.append(buf, n);
  pclose(p);
  while (!out.empty() && (out.back() == '\n' || out.back() == '\r'))
    out.pop_back();
  return out;
}
// Resolve a base Python 3.11 to build the venv from (the Linux equivalent of
// `py -3.11`).
std::string resolveBasePython() {
  for (const char *probe : {"python3.11", "python3", "python"}) {
    std::string r =
        runCapture(std::string(probe) +
                   " -c 'import sys;print(sys.executable)' 2>/dev/null");
    if (fileExists(r))
      return r;
  }
  return "";
}
int runWait(const std::string &cmd, const std::string &cwd) {
  std::string full = cwd.empty() ? cmd : ("cd " + shq(cwd) + " && " + cmd);
  int rc = std::system(full.c_str());
  if (rc == -1)
    return -1;
  return (rc >> 8) & 0xFF; // WEXITSTATUS
}

struct Payload {
  std::string srcDir;
  std::string wheelhouse;
};
bool hasSource(const std::string &d) {
  return fileExists((fs::path(d) / "installer" / "provision.py").string()) &&
         fileExists((fs::path(d) / "requirements.txt").string());
}
bool findPayload(Payload &p) {
  const std::string dir = exeDir();
  if (hasSource((fs::path(dir) / "app").string())) {
    p.srcDir = (fs::path(dir) / "app").string();
    if (dirExists((fs::path(dir) / "wheelhouse").string()))
      p.wheelhouse = (fs::path(dir) / "wheelhouse").string();
    return true;
  }
  std::string repo = env_or("CHUDWARE_REPO", "");
  if (!repo.empty() && hasSource(repo)) {
    p.srcDir = repo;
    if (dirExists((fs::path(repo) / "wheelhouse").string()))
      p.wheelhouse = (fs::path(repo) / "wheelhouse").string();
    return true;
  }
  fs::path d = dir;
  for (int i = 0; i < 6; ++i) {
    if (hasSource(d.string())) {
      p.srcDir = d.string();
      if (dirExists((d / "wheelhouse").string()))
        p.wheelhouse = (d / "wheelhouse").string();
      return true;
    }
    if (!d.has_parent_path() || d.parent_path() == d)
      break;
    d = d.parent_path();
  }
  return false;
}

bool provisionRuntime(bool force, std::string &log) {
  if (!force && runtimeReady()) {
    log = "runtime already present";
    return true;
  }
  Payload pl;
  if (!findPayload(pl)) {
    log = "runtime payload not found - run from a source checkout or set "
          "CHUDWARE_REPO.";
    return false;
  }
  std::string base = resolveBasePython();
  if (base.empty()) {
    log =
        "no base Python 3.11 found - install python3.11 to build the runtime.";
    return false;
  }
  std::string cmd = shq(base) + " -m services.installer.provision --base-python " +
                    shq(base) + " --with-app-code";
  if (force)
    cmd += " --force";
  if (!pl.wheelhouse.empty())
    cmd += " --wheelhouse " + shq(pl.wheelhouse);
  int rc = runWait(cmd, pl.srcDir);
  log = std::string(force ? "repaired runtime" : "provisioned runtime") +
        " (rc=" + std::to_string(rc) + ", base=" + base + ")";
  return rc == 0 && runtimeReady();
}

} // namespace

std::string exeDir() {
  const std::string exe = chudware::platform::executablePath();
  if (exe.empty())
    return ".";
  return fs::path(exe).parent_path().string();
}

std::string runtimePython() {
  std::string envp = env_or("CHUDWARE_PYTHON", "");
  if (!envp.empty() && fileExists(envp))
    return envp;
  std::string installed =
      (fs::path(appdataRoot()) / "runtime" / "venv" / "bin" / "python")
          .string();
  if (fileExists(installed))
    return installed;
  std::string portable =
      (fs::path(exeDir()) / "runtime" / "venv" / "bin" / "python").string();
  if (fileExists(portable))
    return portable;
  return installed; // canonical default for messaging when nothing is installed
                    // yet
}

// The worker boots `python -m services.worker_main` with cwd=appDir(), so a dir
// only qualifies if the interpreter can resolve that module from it.
// Post-cutover checkouts have no top-level engine/ (engine_oracle deleted) —
// probe services/worker_main.py, keep the legacy engine+agent probe for staged
// payloads and old checkouts.
static bool looksLikeAppDir(const fs::path &d) {
  if (fileExists((d / "services" / "worker_main.py").string()))
    return true;
  return dirExists((d / "engine").string()) &&
         dirExists((d / "agent").string()) &&
         fileExists((d / "requirements.txt").string());
}

std::string appDir() {
  std::string repo = env_or("CHUDWARE_REPO", "");
  if (!repo.empty() && looksLikeAppDir(fs::path(repo)))
    return repo;
  std::string portable = (fs::path(exeDir()) / "app").string();
  if (dirExists((fs::path(portable) / "engine").string()) &&
      dirExists((fs::path(portable) / "agent").string()))
    return portable;
  fs::path d = exeDir();
  for (int i = 0; i < 6; ++i) {
    if (looksLikeAppDir(d))
      return d.string();
    if (!d.has_parent_path() || d.parent_path() == d)
      break;
    d = d.parent_path();
  }
  return (fs::path(appdataRoot()) / "app").string();
}

bool runtimeReady() {
  // On POSIX we don't apply the venv-freshness hash gate (the Windows path uses
  // it to catch a stale %APPDATA% venv); presence of the interpreter is enough
  // — see bootstrap.h.
  return fileExists(runtimePython());
}

void ensurePromptsDir() {
  if (!env_or("CHUDWARE_PROMPTS_DIR", "").empty())
    return; // respect an explicit override
  std::string p = (fs::path(appDir()) / "services" / "agent" / "prompts").string();
  if (!dirExists(p))
    return;
  setenv("CHUDWARE_PROMPTS_DIR", p.c_str(), 1);
}

bool ensureRuntime(std::string &log) {
  ensurePromptsDir();
  return provisionRuntime(false, log);
}

bool repairRuntime(std::string &log) { return provisionRuntime(true, log); }

#endif // _WIN32
