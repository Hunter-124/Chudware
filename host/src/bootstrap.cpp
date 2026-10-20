#include "bootstrap.h"

#ifdef _WIN32

#include <windows.h>

#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <set>
#include <string>
#include <vector>


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

// The base executable/platform runtime is authenticated by the operator. Never
// run a candidate, PATH discovery helper, or bundled executable to attest itself.
std::string resolveBasePython() {
  const std::string base = env_or("CHUDWARE_BASE_PYTHON",
                                 env_or("CHUDWARE_CI_BASE_PYTHON", ""));
  const std::filesystem::path path(base);
  std::error_code ec;
  const auto resolved = std::filesystem::canonical(path, ec);
  if (base.empty() || !path.is_absolute() || ec || !fileExists(base) ||
      fileExists((path.parent_path().parent_path() / "pyvenv.cfg").string()) ||
      fileExists((resolved.parent_path().parent_path() / "pyvenv.cfg").string()))
    return "";
  const std::string override = env_or("CHUDWARE_PYTHON", "");
  if (override.empty() || !fileExists(override)) {
    const auto root = std::filesystem::weakly_canonical(
        std::filesystem::path(runtimePython()).parent_path().parent_path(), ec);
    if (ec)
      return "";
    const std::string prefix = root.string() + "\\";
    const std::string executable = resolved.string();
    if (_strnicmp(executable.c_str(), prefix.c_str(), prefix.size()) == 0)
      return "";
  }
  return base;
}
int runWait(const std::string &cmd, const std::string &cwd) {
  STARTUPINFOA si{};
  si.cb = sizeof(si);
  PROCESS_INFORMATION pi{};
  std::vector<char> c(cmd.begin(), cmd.end());
  c.push_back(0);
  std::vector<char> childEnv;
  LPCH inherited = GetEnvironmentStringsA();
  if (!inherited)
    return -1;
  for (const char *entry = inherited; *entry; entry += std::strlen(entry) + 1) {
    if (_strnicmp(entry, "PYTHON", 6) == 0 ||
        _strnicmp(entry, "LD_", 3) == 0 || _strnicmp(entry, "DYLD_", 5) == 0 ||
        _strnicmp(entry, "__PYVENV_LAUNCHER__=", sizeof("__PYVENV_LAUNCHER__=") - 1) == 0 ||
        _strnicmp(entry, "VIRTUAL_ENV=", sizeof("VIRTUAL_ENV=") - 1) == 0)
      continue;
    childEnv.insert(childEnv.end(), entry, entry + std::strlen(entry) + 1);
  }
  FreeEnvironmentStringsA(inherited);
  if (childEnv.empty())
    childEnv.push_back(0);
  childEnv.push_back(0);
  if (!CreateProcessA(nullptr, c.data(), nullptr, nullptr, FALSE, 0, childEnv.data(),
                      cwd.empty() ? nullptr : cwd.c_str(), &si, &pi))
    return -1;
  WaitForSingleObject(pi.hProcess, INFINITE);
  DWORD code = 1;
  GetExitCodeProcess(pi.hProcess, &code);
  CloseHandle(pi.hProcess);
  CloseHandle(pi.hThread);
  return (int)code;
}

// A development source payload requires reviewed offline dependency identities;
// packaged customer sidecars do not use this provisioner.
struct Payload {
  std::string srcDir;
  std::string wheelhouse;
};
bool hasSource(const std::string &d) {
  return fileExists(d + "\\services\\installer\\provision.py") &&
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


// Reuse requires the full lock/target and installed PEP 610 identities, not
// merely a matching declaration hash or an importable private-package version.
bool installedVenvFresh() {
  Payload pl;
  if (!findPayload(pl) || pl.wheelhouse.empty() ||
      !fileExists(pl.srcDir + "\\vendor\\python\\RUNTIME_WHEELS.lock") ||
      !fileExists(pl.srcDir + "\\vendor\\chudcode\\CHUDCODE_ARTIFACT.lock"))
    return false;
  const std::string py = runtimePython();
  const std::string venv = std::filesystem::path(py).parent_path().parent_path().string();
  const std::string base = resolveBasePython();
  if (base.empty())
    return false;
  std::string cmd = "\"" + base +
                    "\" -I -S -c \"import sys;sys.path.insert(0,sys.argv[1]);"
                    "from services.installer.provision import main;sys.exit(main(sys.argv[2:]))\" \"" +
                    pl.srcDir + "\" --check-ready --base-python \"" + base +
                    "\" --venv-dir \"" + venv + "\" --wheelhouse \"" + pl.wheelhouse + "\"";
  return runWait(cmd, pl.srcDir) == 0;
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
  if (pl.wheelhouse.empty() ||
      !fileExists(pl.srcDir + "\\vendor\\python\\RUNTIME_WHEELS.lock") ||
      !fileExists(pl.srcDir + "\\vendor\\chudcode\\CHUDCODE_ARTIFACT.lock")) {
    log = "reviewed runtime wheelhouse/lock missing; authenticate all runtime wheels "
          "and vendor/python/RUNTIME_WHEELS.lock before provisioning.";
    return false;
  }
  std::string base = resolveBasePython();
  if (base.empty()) {
    log = "set CHUDWARE_BASE_PYTHON to a separately authenticated absolute base "
          "Python path (including its stdlib/platform runtime).";
    return false;
  }
  std::string cmd = "\"" + base +
      "\" -I -S -c \"import sys;sys.path.insert(0,sys.argv[1]);"
      "from services.installer.provision import main;sys.exit(main(sys.argv[2:]))\" \"" +
      pl.srcDir + "\" --base-python \"" + base + "\" --with-app-code";
  if (force)
    cmd += " --force";
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
  // Explicit developer interpreter overrides are caller-owned, not managed
  // provisioning. Automatically discovered runtimes require locked identities.
  std::string envp = env_or("CHUDWARE_PYTHON", "");
  if (!envp.empty() && fileExists(envp))
    return true;
  return installedVenvFresh();
}

// Developer builds point libchudware at authored prompt files. Packaged builds
// use the compiled protected-assets table and must never discover built-ins from
// an environment variable or install-directory filesystem.
void ensurePromptsDir() {
#ifdef CHUDWARE_DEVELOPER_BUILD
  if (!env_or("CHUDWARE_PROMPTS_DIR", "").empty())
    return;
  std::string p = appDir() + "\\agent\\prompts";
  if (!dirExists(p))
    return;
  _putenv_s("CHUDWARE_PROMPTS_DIR", p.c_str());
  SetEnvironmentVariableA("CHUDWARE_PROMPTS_DIR", p.c_str());
#endif
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
#include <spawn.h>
#include <sys/wait.h>

#include <cstdlib>
#include <filesystem>
#include <set>
#include <string>
#include <vector>
#include <cstring>
#include <cerrno>

extern char **environ;

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
std::string resolveBasePython() {
  const std::string base = env_or("CHUDWARE_BASE_PYTHON",
                                 env_or("CHUDWARE_CI_BASE_PYTHON", ""));
  const fs::path path(base);
  std::error_code ec;
  const auto resolved = fs::canonical(path, ec);
  if (base.empty() || !path.is_absolute() || ec || !fileExists(base) ||
      fileExists((path.parent_path().parent_path() / "pyvenv.cfg").string()) ||
      fileExists((resolved.parent_path().parent_path() / "pyvenv.cfg").string()))
    return "";
  const std::string override = env_or("CHUDWARE_PYTHON", "");
  if (override.empty() || !fileExists(override)) {
    const auto root = fs::weakly_canonical(fs::path(runtimePython()).parent_path().parent_path(), ec);
    if (ec)
      return "";
    const auto relative = resolved.lexically_relative(root);
    if (!relative.empty() && *relative.begin() != "..")
      return "";
  }
  return base;
}
int runWait(const std::string &cmd, const std::string &cwd) {
  std::string full = cwd.empty() ? cmd : ("cd " + shq(cwd) + " && " + cmd);
  // Strip loader overrides before even /bin/sh is loaded; an `env -u` inside
  // std::system would be too late to prevent LD_PRELOAD/DYLD injection.
  std::vector<char *> childEnv;
  for (char **entry = environ; entry && *entry; ++entry) {
    if (std::strncmp(*entry, "PYTHON", 6) == 0 ||
        std::strncmp(*entry, "LD_", 3) == 0 || std::strncmp(*entry, "DYLD_", 5) == 0 ||
        std::strncmp(*entry, "__PYVENV_LAUNCHER__=", sizeof("__PYVENV_LAUNCHER__=") - 1) == 0 ||
        std::strncmp(*entry, "VIRTUAL_ENV=", sizeof("VIRTUAL_ENV=") - 1) == 0)
      continue;
    childEnv.push_back(*entry);
  }
  childEnv.push_back(nullptr);
  char *args[] = {const_cast<char *>("/bin/sh"), const_cast<char *>("-c"), full.data(), nullptr};
  pid_t child = 0;
  if (posix_spawn(&child, "/bin/sh", nullptr, nullptr, args, childEnv.data()) != 0)
    return -1;
  int status = 0;
  while (waitpid(child, &status, 0) == -1) {
    if (errno != EINTR)
      return -1;
  }
  return WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

struct Payload {
  std::string srcDir;
  std::string wheelhouse;
};
bool hasSource(const std::string &d) {
  return fileExists((fs::path(d) / "services" / "installer" / "provision.py").string()) &&
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
  if (pl.wheelhouse.empty() ||
      !fileExists((fs::path(pl.srcDir) / "vendor/python/RUNTIME_WHEELS.lock").string()) ||
      !fileExists((fs::path(pl.srcDir) / "vendor/chudcode/CHUDCODE_ARTIFACT.lock").string())) {
    log = "reviewed runtime wheelhouse/lock missing; authenticate all runtime wheels "
          "and vendor/python/RUNTIME_WHEELS.lock before provisioning.";
    return false;
  }
  std::string base = resolveBasePython();
  if (base.empty()) {
    log = "set CHUDWARE_BASE_PYTHON to a separately authenticated absolute base "
          "Python path (including its stdlib/platform runtime).";
    return false;
  }
  std::string cmd = shq(base) + " -I -S -c " +
      shq("import sys;sys.path.insert(0,sys.argv[1]);"
          "from services.installer.provision import main;sys.exit(main(sys.argv[2:]))") +
      " " + shq(pl.srcDir) + " --base-python " + shq(base) + " --with-app-code";
  if (force)
    cmd += " --force";
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
  const std::string py = runtimePython();
  if (!fileExists(py))
    return false;
  // An explicit developer interpreter is outside managed provisioning.
  const std::string override = env_or("CHUDWARE_PYTHON", "");
  if (!override.empty() && fileExists(override))
    return true;
  Payload pl;
  if (!findPayload(pl) || pl.wheelhouse.empty() ||
      !fileExists((fs::path(pl.srcDir) / "vendor/python/RUNTIME_WHEELS.lock").string()) ||
      !fileExists((fs::path(pl.srcDir) / "vendor/chudcode/CHUDCODE_ARTIFACT.lock").string()))
    return false;
  const std::string base = resolveBasePython();
  if (base.empty())
    return false;
  const std::string cmd = shq(base) +
      " -I -S -c " + shq("import sys;sys.path.insert(0,sys.argv[1]);"
                          "from services.installer.provision import main;sys.exit(main(sys.argv[2:]))") +
      " " + shq(pl.srcDir) + " --check-ready --base-python " + shq(base) + " --venv-dir " +
      shq(fs::path(py).parent_path().parent_path().string()) +
      " --wheelhouse " + shq(pl.wheelhouse);
  return runWait(cmd, pl.srcDir) == 0;
}

void ensurePromptsDir() {
#ifdef CHUDWARE_DEVELOPER_BUILD
  if (!env_or("CHUDWARE_PROMPTS_DIR", "").empty())
    return;
  std::string p = (fs::path(appDir()) / "services" / "agent" / "prompts").string();
  if (!dirExists(p))
    return;
  setenv("CHUDWARE_PROMPTS_DIR", p.c_str(), 1);
#endif
}

bool ensureRuntime(std::string &log) {
  ensurePromptsDir();
  return provisionRuntime(false, log);
}

bool repairRuntime(std::string &log) { return provisionRuntime(true, log); }

#endif // _WIN32
