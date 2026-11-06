// Chudware shared application state + data model — implementations (extracted from gui_app.cpp).
#include "gl_compat.h"
//
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "stb_image.h"
#include "stb_image_resize2.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cctype>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <mutex>
#include <set>
#include <thread>

// Platform primitives for the config.json advisory lock (see updateConfigJson below).
#if defined(_WIN32)
#include <windows.h>
#elif !defined(__EMSCRIPTEN__)
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
#else
#include <unistd.h>  // ::getpid() — the config-write tmp name below uses it on all non-Windows
#endif

#include "app_state.h"
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#include "fs_backend.h"
#endif

#include "../bootstrap.h"
#include "cpp_core.h"  // in-process C++ core (libchudware) — the backend when no Python runtime
#include "engine_backend.h"
#include "engine_events.h"
#include "../mesh.h"
#include "../renderer.h"

#include "../render/aicm_loader.h"
#include "../render/id_pass.h"
#include "../render/overlays.h"

#include "../ui/feature_tree.h"  // W1-B: ui::featureTreeResetTransient (clear tree statics on doc change)
#include "../ui/anim.h"
#include "../ui/sketch_mode.h"   // ui::SketchMode::clearEntitySelection (clearSelection)
#include "../ui/sketch3d_mode.h"
#include "../ui/property_manager.h"
#include "../ui/plm/plm_hub.h"   // ui::plmInvalidate (unified working-dir switch)

const char* kStarterCode =
    "# build123d is imported. Assign the final solid to `result`.\n"
    "with BuildPart() as p:\n"
    "    Box(60, 40, 12)\n"
    "    fillet(p.edges().filter_by(Axis.Z), radius=5)\n"
    "    with Locations((-18, 0, 6), (18, 0, 6)):\n"
    "        CounterBoreHole(2.6, 5, 3, depth=12)\n"
    "result = p.part\n";

// ---------------- settings (AI provider) ----------------
std::string appdataDir() {
#ifdef __EMSCRIPTEN__
    // Durable IDBFS mount (see appstateInitPersist). Path is stable before mount
    // completes; first load/save waits on appstatePersistReady().
    return "/persist";
#elif defined(_WIN32)
    const char* a = std::getenv("APPDATA");
    return std::string(a ? a : ".");
#else
    // Parent of the Chudware data dir (callers append "/Chudware/..."): XDG data home, else
    // ~/.local/share — matching bootstrap.cpp's appdataRoot() (~/.local/share/Chudware).
    if (const char* x = std::getenv("XDG_DATA_HOME")) { if (*x) return std::string(x); }
    if (const char* h = std::getenv("HOME")) { if (*h) return std::string(h) + "/.local/share"; }
    return ".";
#endif
}
// Chudware is now the UNCONDITIONAL data dir. Machines whose data still sits under a
// pre-rename root ("Billet" / "AI-CAD") get it copied over once (copy-if-missing merge; the
// old dir is never moved or deleted); if the copy can't complete we keep using the legacy
// root for that boot so nothing breaks. Managed venvs and heavy payloads are skipped —
// venvs do not survive relocation and payloads re-provision; absolute paths inside the old
// tree (e.g. a llamacpp model_path) stay valid because the old tree is left in place.
static bool chudwareCopyTreeIfMissing(const std::filesystem::path& src,
                                      const std::filesystem::path& dst) {
    namespace fs = std::filesystem;
    std::error_code ec;
    if (fs::is_regular_file(src, ec)) {
        if (fs::exists(dst, ec)) return true;
        fs::create_directories(dst.parent_path(), ec);
        ec.clear();
        fs::copy_file(src, dst, ec);
        // A concurrent process winning the same copy is not a failure.
        if (ec && fs::exists(dst, ec)) ec.clear();
        return !ec;
    }
    fs::create_directories(dst, ec);
    bool ok = !ec;
    for (const auto& de : fs::directory_iterator(src, ec))
        ok = chudwareCopyTreeIfMissing(de.path(), dst / de.path().filename()) && ok;
    if (ec) ok = false;
    return ok;
}

// One-time appdata migration: copy a legacy install (marker: config.json) into the
// Chudware root, skipping venvs / re-provisionable payloads. Returns false (caller keeps
// using the legacy root) if anything couldn't be copied.
static bool chudwareMigrateLegacyAppdata(const std::filesystem::path& oldRoot,
                                         const std::filesystem::path& newRoot) {
    namespace fs = std::filesystem;
    static const std::set<std::string> kSkip = {
        "runtime",   "mcp-venv",  "torch-venv",  // venvs: absolute paths baked in
        "models",    "llamacpp",                 // GBs of re-downloadable payloads
        "cache",     "prof",                     // regenerable
        "cef_cache", "cef_cache_daemon", "cef_cache_gui",
    };
    std::error_code ec;
    fs::create_directories(newRoot, ec);
    bool ok = !ec;
    for (const auto& de : fs::directory_iterator(oldRoot, ec)) {
        if (kSkip.count(de.path().filename().string())) continue;
        ok = chudwareCopyTreeIfMissing(de.path(), newRoot / de.path().filename()) && ok;
    }
    if (ec) ok = false;
    return ok;
}

std::string appdataAppDir() {
    static const std::string cached = [] {
        namespace fs = std::filesystem;
        const fs::path base = appdataDir();
        const fs::path neu = base / "Chudware";
        std::error_code ec;
        std::string chosen = neu.string();
        if (!fs::exists(neu / "config.json", ec)) {
            // Legacy install present? Copy it forward once (newest legacy name first);
            // only a FAILED copy keeps the legacy root in use.
            for (const char* legacy : {"Billet", "AI-CAD"}) {
                const fs::path old = base / legacy;
                if (!fs::exists(old / "config.json", ec)) continue;
                if (chudwareMigrateLegacyAppdata(old, neu)) {
                    std::fprintf(stderr, "[chudware] migrated data dir %s -> %s\n",
                                 old.string().c_str(), neu.string().c_str());
                } else {
                    chosen = old.string();
                }
                break;
            }
        }
        std::fprintf(stderr, "[chudware] data dir: %s\n", chosen.c_str());
        return chosen;
    }();
    return cached;
}
std::string configPath() { return appdataAppDir() + "/config.json"; }

#ifdef __EMSCRIPTEN__
// IDBFS at /persist so config.json + chat_history.json survive page reloads.
// Mount + populate once before any appdata read; flush (debounced) after writes.
static std::atomic<bool> g_persistReady{false};

void appstateInitPersist() {
    if (g_persistReady.load(std::memory_order_acquire)) return;
    EM_ASM({
        try {
            var root = '/persist';
            Module.__chudwarePersistReady = false;
            try { FS.mkdir(root); } catch (e) {}
            try { FS.mount(IDBFS, {}, root); } catch (e) {
                // Already mounted on a hot restart — keep going and re-populate.
                console.warn('[chudware] IDBFS mount:', e);
            }
            // Flush a queued debounce as soon as the page leaves the foreground.
            // IndexedDB remains asynchronous, but it must at least start before unload.
            if (!Module.__chudwarePersistHideHook) {
                Module.__chudwarePersistHideHook = true;
                var flushPending = function() {
                    if (Module.__chudwarePersistSyncTimer) {
                        clearTimeout(Module.__chudwarePersistSyncTimer);
                        Module.__chudwarePersistSyncTimer = null;
                        if (Module.__chudwarePersistFlush) Module.__chudwarePersistFlush();
                    }
                };
                window.addEventListener('pagehide', flushPending);
                document.addEventListener('visibilitychange', function() {
                    if (document.visibilityState === 'hidden') flushPending();
                });
            }
            FS.syncfs(true, function(err) {
                if (err) console.warn('[chudware] IDBFS populate failed:', err);
                else console.log('[chudware] IDBFS populated at /persist');
                // Ensure Chudware appdata subdir exists after populate (empty store).
                try { FS.mkdir(root + '/Chudware'); } catch (e2) {}
                Module.__chudwarePersistReady = true;
            });
        } catch (e) {
            console.error('[chudware] IDBFS init failed:', e);
            Module.__chudwarePersistReady = true;  // fail open so boot continues
        }
    });
    // ASYNCIFY: spin until the populate callback fires so loadSettings/chat never
    // race an empty in-memory mount and clobber IndexedDB on the first write.
    while (!EM_ASM_INT({ return Module.__chudwarePersistReady ? 1 : 0; })) {
        emscripten_sleep(10);
    }
    g_persistReady.store(true, std::memory_order_release);
}

bool appstatePersistReady() {
    return g_persistReady.load(std::memory_order_acquire);
}

void appstatePersistSync() {
    if (!g_persistReady.load(std::memory_order_acquire)) return;
    // Debounce idle writes, but mark a single trailing flush immediately when a
    // write arrives during syncfs. Every flush therefore completes before the next starts.
    EM_ASM({
        if (!Module.__chudwarePersistFlush) {
            Module.__chudwarePersistFlush = function() {
                if (Module.__chudwarePersistSyncInFlight) {
                    Module.__chudwarePersistSyncPending = true;
                    return;
                }
                Module.__chudwarePersistSyncInFlight = true;
                FS.syncfs(false, function(err) {
                    Module.__chudwarePersistSyncInFlight = false;
                    if (err) console.warn('[chudware] IDBFS flush failed:', err);
                    if (Module.__chudwarePersistSyncPending) {
                        Module.__chudwarePersistSyncPending = false;
                        Module.__chudwarePersistFlush();
                    }
                });
            };
        }
        if (Module.__chudwarePersistSyncInFlight) {
            Module.__chudwarePersistSyncPending = true;
        } else {
            if (Module.__chudwarePersistSyncTimer)
                clearTimeout(Module.__chudwarePersistSyncTimer);
            Module.__chudwarePersistSyncTimer = null;
            if (document.visibilityState === 'hidden') {
                Module.__chudwarePersistFlush();
            } else {
                Module.__chudwarePersistSyncTimer = setTimeout(function() {
                    Module.__chudwarePersistSyncTimer = null;
                    Module.__chudwarePersistFlush();
                }, 50);
            }
        }
    });
}

namespace {
std::mutex g_recentFilesPersistMutex;
std::vector<std::string> g_lastRecentFiles;
bool g_recentFilesBaselineReady = false;

void primeRecentFilesPersistence(const std::vector<std::string>& files) {
    std::lock_guard<std::mutex> guard(g_recentFilesPersistMutex);
    g_lastRecentFiles = files;
    g_recentFilesBaselineReady = true;
}

void persistRecentFilesIfChanged(const std::vector<std::string>& files) {
    std::lock_guard<std::mutex> guard(g_recentFilesPersistMutex);
    // loadSettings normally primes this baseline. If an unusual caller saves first,
    // observe rather than misclassifying an unrelated settings save as a recents edit.
    if (!g_recentFilesBaselineReady) {
        g_lastRecentFiles = files;
        g_recentFilesBaselineReady = true;
        return;
    }
    if (g_lastRecentFiles == files) return;

    std::string err;
    if (chudware::fsBackend().saveRecents(files, err)) g_lastRecentFiles = files;
}
}  // namespace
#else
void appstateInitPersist() {}
bool appstatePersistReady() { return true; }
void appstatePersistSync() {}
#endif

// Default workspace dir under Documents: always Documents/Chudware (created on first use by
// the File Explorer). Pre-rename Documents/Billet or Documents/AI-CAD content is copied
// forward once (copy-if-missing merge; the old dir is left untouched). Falls back to the
// appdata base when no home/Documents resolves.
static void chudwareMergeSessionIndex(const std::filesystem::path& srcDir,
                                      const std::filesystem::path& dstDir) {
    // Union-merge the v2 session envelope: keep the target's order, append ids only the
    // legacy index lists, keep the target's active id (fall back to the legacy one). Orphan
    // transcript files are invisible to the loader, so the index must name every copied id.
    namespace fs = std::filesystem;
    namespace json = nlohmann;
    auto readIndex = [](const fs::path& p) {
        json::json j = json::json::object();
        std::ifstream in(p);
        if (in) { try { in >> j; } catch (...) { j = json::json::object(); } }
        return j;
    };
    const json::json dstIdx = readIndex(dstDir / "index.json");
    const json::json srcIdx = readIndex(srcDir / "index.json");
    if (!srcIdx.is_object() || !srcIdx.contains("order")) return;  // nothing to merge
    std::set<std::string> seen;
    json::json order = json::json::array();
    auto pushId = [&](const std::string& id) {
        if (!id.empty() && seen.insert(id).second) order.push_back(id);
    };
    if (dstIdx.is_object() && dstIdx.contains("order") && dstIdx["order"].is_array())
        for (const auto& j : dstIdx["order"])
            if (j.is_string()) pushId(j.get<std::string>());
    if (srcIdx["order"].is_array())
        for (const auto& j : srcIdx["order"])
            if (j.is_string()) pushId(j.get<std::string>());
    std::string active = dstIdx.value("active", std::string());
    if (active.empty()) active = srcIdx.value("active", std::string());
    // Keep every other key the target index carries (v3 `active_by_project`, `session_seq`,
    // `version`). This merge runs on EVERY launch while a legacy dir exists, and rebuilding a
    // bare v2 envelope here silently dropped them each time (2026-09-27).
    json::json merged = dstIdx.is_object() ? dstIdx : json::json::object();
    if (!merged.contains("version")) merged["version"] = 2;
    merged["active"] = active;
    merged["order"] = order;
    std::error_code ec;
    fs::create_directories(dstDir, ec);
    std::ofstream out(dstDir / "index.json", std::ios::trunc);
    if (out) out << merged.dump(1);
}

static void chudwareMigrateLegacyWorkspace(const std::filesystem::path& base) {
    namespace fs = std::filesystem;
    std::error_code ec;
    for (const char* legacy : {"Billet", "AI-CAD"}) {
        const fs::path src = base / legacy;
        if (!fs::exists(src, ec)) continue;
        for (const auto& de : fs::directory_iterator(src, ec)) {
            const fs::path to = base / "Chudware" / de.path().filename();
            if (de.path().filename() == "Sessions" && de.is_directory(ec)) {
                chudwareCopyTreeIfMissing(de.path(), to);
                chudwareMergeSessionIndex(de.path(), to);
            } else {
                chudwareCopyTreeIfMissing(de.path(), to);
            }
        }
        ec.clear();
    }
}

std::string defaultWorkspaceDir() {
#ifdef __EMSCRIPTEN__
    return "/workspace";
#else
    namespace fs = std::filesystem;
#ifdef _WIN32
    const char* up = std::getenv("USERPROFILE");
    const fs::path base = up && *up ? fs::path(up) / "Documents" : fs::path(appdataDir());
#else
    const char* home = std::getenv("HOME");
    const fs::path base = home && *home ? fs::path(home) / "Documents" : fs::path(appdataDir());
#endif
    static const bool migrated = [&] {
        chudwareMigrateLegacyWorkspace(base);
        return true;
    }();
    (void)migrated;
    return (base / "Chudware").string();
#endif
}
const char* kDefaultLlamaModelPath = "%APPDATA%\\Chudware\\models\\model.gguf";

static void copyStr(char* dst, size_t cap, const std::string& v) {
    std::strncpy(dst, v.c_str(), cap - 1);
    dst[cap - 1] = '\0';
}

// Canonical config.json "appearance.glass" group keys — index == (size_t)ui::GlassGroup.
// Shared by loadSettings/saveSettings so the schema can never diverge between the two.
static const char* kGlassGroupKey[] = {"global", "feature_manager", "copilot", "hud"};

namespace {
nlohmann::json readConfigFile() {
    try {
        std::ifstream in(configPath());
        nlohmann::json config;
        if (in) {
            in >> config;
            if (config.is_object()) return config;
        }
    } catch (...) {
    }
    return nlohmann::json::object();
}
}  // namespace

void loadSettings(Settings& s) {
    nlohmann::json j = readConfigJson();
    if (j.empty()) {
#ifdef __EMSCRIPTEN__
        // Web: no config yet under IDBFS — still hydrate recents from the workspace.
        std::string rerr;
        auto durable = chudware::fsBackend().loadRecents(rerr);
        if (!durable.empty()) s.recentFiles = std::move(durable);
        primeRecentFilesPersistence(s.recentFiles);
#endif
        return;
    }
    // One malformed value (a null or a string where a number is expected) used to abort the whole
    // load, leaving every later field at its default — and the next saveSettings wrote those
    // defaults back over the user's real values. Sections below are therefore guarded
    // independently; `safely` guards a single scalar.
    auto safely = [](const auto& fn) {
        try { fn(); } catch (...) {}
    };
    try {
        s.composerFollowRoles = j.value("composer_follow_roles",
                                        !(j.contains("provider") || j.contains("model")));
        std::string prov = j.value("provider", std::string());
        s.ompProvider = j.value("omp_provider", prov);
        auto oai = j.value("openai", nlohmann::json::object());
        auto llama = j.value("llamacpp", nlohmann::json::object());
        auto fla = j.value("freellmapi", nlohmann::json::object());
        std::string base = oai.value("base_url", "");
        bool hasBase = !base.empty();
        // A base_url whose host is loopback => a LOCAL endpoint; any other host => a cloud
        // OpenAI-compatible API (DashScope / DeepSeek / ...). This split is what lets the offline
        // (ITAR) guard refuse the cloud slot while keeping local allowed.
        auto isLoopbackUrl = [](const std::string& u) {
            return u.find("localhost") != std::string::npos ||
                   u.find("127.0.0.1") != std::string::npos ||
                   u.find("0.0.0.0") != std::string::npos ||
                   u.find("[::1]") != std::string::npos || u.find("://::1") != std::string::npos;
        };
        bool baseLoopback = hasBase && isLoopbackUrl(base);
        if (prov == "gemini") s.provider = 4;
        else if (prov == "anthropic") s.provider = 0;
        else if (prov == "llamacpp" || prov == "llama.cpp" || prov == "llama-cpp") s.provider = 3;
        else if (prov == "xai" || prov == "xai-sub") s.provider = 6;  // PROV_XAI
        else if (prov == "cursor" || prov == "cursor-sub") s.provider = 7;  // PROV_CURSOR
        else if (prov == "kimi-sub") s.provider = 8;  // PROV_KIMISUB (Kimi Code subscription OAuth)
        else if (prov == "zenmux") s.provider = 9;  // PROV_ZENMUX (Chudcode — default provider)
        else if (prov == "freellmapi") s.provider = 10;  // PROV_FREELLMAPI (Free AI, native wire)
        else if (prov == "moonshot") {
            // First-class Moonshot (Kimi) wire provider — GUI still uses the cloud-compat slot
            // (PROV_CLOUDCOMPAT=5) so the model picker / base_url path stays shared.
            s.provider = 5;
            if (!hasBase) {
                base = "https://api.moonshot.ai/v1";
                hasBase = true;
            }
        }
        else if (hasBase) s.provider = baseLoopback ? 2 : 5;  // 2=Local, 5=Cloud OpenAI-compatible
        else s.provider = 1;                                   // 1=OpenAI cloud (api.openai.com)
        if (s.provider == 2 || s.provider == 3) s.ompProvider.clear();
        std::string model = j.value("model", "");
        if (s.provider == 3) model = llama.value("model_path", model);
        if (s.provider == 3 && model.empty()) model = kDefaultLlamaModelPath;
        if (s.provider == 10 && model.empty()) model = fla.value("model", std::string("auto"));
        copyStr(s.model, sizeof(s.model), model);
        std::string activeKey = j.value("api_key", "");
        copyStr(s.apiKey, sizeof(s.apiKey), activeKey);

        // Per-provider keys (config.json "keys"), so the picker can hide unkeyed providers.
        auto keys = j.value("keys", nlohmann::json::object());
        copyStr(s.geminiKey, sizeof(s.geminiKey), keys.value("gemini", std::string()));
        copyStr(s.anthropicKey, sizeof(s.anthropicKey), keys.value("anthropic", std::string()));
        copyStr(s.openaiKey, sizeof(s.openaiKey), keys.value("openai", std::string()));
        copyStr(s.cloudKey, sizeof(s.cloudKey), keys.value("cloud_compat", std::string()));
        copyStr(s.moonshotKey, sizeof(s.moonshotKey), keys.value("moonshot", std::string()));
        copyStr(s.xaiKey, sizeof(s.xaiKey), keys.value("xai", std::string()));
        copyStr(s.freellmapiKey, sizeof(s.freellmapiKey), keys.value("freellmapi", std::string()));
        // Back-compat: fold the active provider's top-level api_key into its per-provider slot.
        if (!activeKey.empty()) {
            if (prov == "gemini" && !s.geminiKey[0]) copyStr(s.geminiKey, sizeof(s.geminiKey), activeKey);
            else if (prov == "anthropic" && !s.anthropicKey[0]) copyStr(s.anthropicKey, sizeof(s.anthropicKey), activeKey);
            else if (prov == "openai" && !hasBase && !s.openaiKey[0]) copyStr(s.openaiKey, sizeof(s.openaiKey), activeKey);
            else if (prov == "openai" && hasBase && !baseLoopback && !s.cloudKey[0]) copyStr(s.cloudKey, sizeof(s.cloudKey), activeKey);
            else if (prov == "moonshot" && !s.moonshotKey[0]) copyStr(s.moonshotKey, sizeof(s.moonshotKey), activeKey);
            else if ((prov == "xai" || prov == "xai-sub") && !s.xaiKey[0]) copyStr(s.xaiKey, sizeof(s.xaiKey), activeKey);
        }
        copyStr(s.reasoningEffort, sizeof(s.reasoningEffort), j.value("reasoning_effort", std::string()));
        if (s.provider == 3) {
            std::string lbase = llama.value("base_url", "http://127.0.0.1:1234/v1");
            std::strncpy(s.baseUrl, lbase.c_str(), sizeof(s.baseUrl) - 1);
            s.vision = llama.value("vision", false);
        } else if (s.provider == 10) {
            std::string flabase = fla.value("base_url", std::string("http://localhost:3001/v1"));
            std::strncpy(s.baseUrl, flabase.c_str(), sizeof(s.baseUrl) - 1);
            s.vision = fla.value("vision", true);
        } else if (hasBase) {
            std::strncpy(s.baseUrl, base.c_str(), sizeof(s.baseUrl) - 1);
            s.vision = oai.value("vision", false);
        }
        s.offlineMode = j.value("offline_mode", false);
        {
            std::string cm = j.value("compliance_mode", std::string("off"));
            std::snprintf(s.complianceMode, sizeof(s.complianceMode), "%s", cm.c_str());
        }
    } catch (...) {
    }
    try {
        // Preference enums (off|option|encourage|require). Migrate legacy plan_mode bool.
        auto parsePref = [](const nlohmann::json& jn, const char* key, int def) -> int {
            if (!jn.contains(key)) return def;
            const auto& v = jn[key];
            if (v.is_number_integer()) {
                int n = v.get<int>();
                return (n < 0 || n > 3) ? def : n;
            }
            if (v.is_string()) {
                const std::string s = v.get<std::string>();
                if (s == "off" || s == "0") return 0;
                if (s == "option" || s == "optional" || s == "1") return 1;
                if (s == "encourage" || s == "recommended" || s == "2") return 2;
                if (s == "require" || s == "required" || s == "3") return 3;
            }
            if (v.is_boolean()) return v.get<bool>() ? 2 : 0;
            return def;
        };
        if (j.contains("plan_preference"))
            s.planPreference = parsePref(j, "plan_preference", 0);
        else if (j.contains("plan_mode"))
            s.planPreference = j.value("plan_mode", false) ? 2 : 0;
        else
            s.planPreference = 0;  // default Off — direct agent, no forced plan/intake
        s.planMode = (s.planPreference != 0);
        s.subagentPreference = parsePref(j, "subagent_preference", 1);
        s.taskListPreference = parsePref(j, "task_list_preference", 2);
        s.advisorPreference = parsePref(j, "advisor_preference", 2);
        // Nested agent.* also accepted (overlay / Python write path).
        if (j.contains("agent") && j["agent"].is_object()) {
            const auto& ag = j["agent"];
            if (ag.contains("subagent_preference"))
                s.subagentPreference = parsePref(ag, "subagent_preference", s.subagentPreference);
            if (ag.contains("task_list_preference"))
                s.taskListPreference = parsePref(ag, "task_list_preference", s.taskListPreference);
            if (ag.contains("advisor_preference"))
                s.advisorPreference = parsePref(ag, "advisor_preference", s.advisorPreference);
            if (ag.contains("proactive_advisor"))
                s.proactiveAdvisor = ag.value("proactive_advisor", false);
            if (ag.contains("memory_enabled"))
                s.memoryEnabled = ag.value("memory_enabled", true);
            if (ag.contains("memory_token_budget")) {
                int b = ag.value("memory_token_budget", 400);
                s.memoryTokenBudget = (b < 50) ? 50 : (b > 4000 ? 4000 : b);
            }
        }
        if (j.contains("proactive_advisor"))
            s.proactiveAdvisor = j.value("proactive_advisor", false);
        if (j.contains("memory_enabled"))
            s.memoryEnabled = j.value("memory_enabled", true);
        if (j.contains("memory_token_budget")) {
            int b = j.value("memory_token_budget", 400);
            s.memoryTokenBudget = (b < 50) ? 50 : (b > 4000 ? 4000 : b);
        }
    } catch (...) {
    }
    try {
        {
            // A config from before the backend choice keeps its meaning: memory_enabled
            // false is "off"; otherwise the default backend.
            std::string backend;
            if (j.contains("memory_backend") && j["memory_backend"].is_string())
                backend = j["memory_backend"].get<std::string>();
            else if (j.contains("agent") && j["agent"].is_object() &&
                     j["agent"].contains("memory_backend") &&
                     j["agent"]["memory_backend"].is_string())
                backend = j["agent"]["memory_backend"].get<std::string>();
            if (backend == "mnemo") backend = "mnemopi";
            if (!backend.empty())
                s.memoryBackend = backend;
            else if (!s.memoryEnabled)
                s.memoryBackend = "off";
            s.memoryEnabled = s.memoryBackend != "off";
        }
        {
            // Accept string names or a persisted int from any build; legacy swarm/edit/eng_firm
            // collapse to Agent (those lanes are the model's own decision now, not a mode).
            int cm = 0;
            if (j.contains("copilot_mode")) {
                const auto& v = j["copilot_mode"];
                if (v.is_number_integer()) {
                    cm = normalizeCopilotMode(v.get<int>());
                } else if (v.is_string()) {
                    const std::string m = v.get<std::string>();
                    if (m == "plan") cm = (int)CopilotMode::Plan;
                    else if (m == "ask" || m == "chat") cm = (int)CopilotMode::Ask;
                    else cm = (int)CopilotMode::Agent;
                }
            }
            s.copilotMode = cm;
        }
        safely([&] { s.maxTokens = j.value("max_output_tokens", 0); });
        safely([&] { s.contextWindow = j.value("context_window", 0); });
        safely([&] { s.systemPrompt = j.value("system_prompt", ""); });
        safely([&] { s.theme = j.value("theme", "light"); });
        safely([&] { s.orthographic = j.value("orthographic", true); });
        safely([&] {
            s.displayQuality = j.value("display_quality", std::string("fine"));
            if (s.displayQuality != "draft" && s.displayQuality != "standard" &&
                s.displayQuality != "fine")
                s.displayQuality = "fine";
        });
        safely([&] { s.sketchDragSnapRelations = j.value("sketch_drag_snap_relations", true); });
        safely([&] {
            s.units = (j.value("units", std::string("mmgs")) == "ips") ? Units::IPS : Units::MMGS;
        });
        safely([&] { s.partsGridView = j.value("parts_grid_view", false); });
        safely([&] { s.partsSortMode = j.value("parts_sort_mode", 0); });
        s.partsExpandedCats.clear();
        if (auto it = j.find("parts_expanded_categories"); it != j.end() && it->is_array())
            for (const auto& e : *it)
                if (e.is_string()) s.partsExpandedCats.push_back(e.get<std::string>());
    } catch (...) {
    }
    try {
        // v2.1 colored liquid glass ("appearance.glass"). Absent keys stay at their GlassStyle
        // sentinels (= inherit Global / built-in default: dynamic color ON, geometry as today),
        // so an old config.json with no "appearance" block loads to the out-of-box look.
        s.glassCfg = ui::GlassConfig{};
        {
            auto app = j.value("appearance", nlohmann::json::object());
            auto gl  = app.is_object() ? app.value("glass", nlohmann::json::object())
                                       : nlohmann::json::object();
            if (gl.is_object()) {
                for (size_t gi = 0; gi < (size_t)ui::GlassGroup::Count; ++gi) {
                    auto go = gl.value(kGlassGroupKey[gi], nlohmann::json::object());
                    if (!go.is_object()) continue;
                    ui::GlassStyle& g = s.glassCfg.group[gi];
                    g.tintHue           = go.value("tint_hue", g.tintHue);
                    g.tintSat           = go.value("tint_sat", g.tintSat);
                    g.tintStrength      = go.value("tint_strength", g.tintStrength);
                    g.glassLevel        = go.value("glass_level", g.glassLevel);
                    g.opacityMul        = go.value("opacity", g.opacityMul);
                    g.rounding          = go.value("rounding", g.rounding);
                    g.textContrastBoost = go.value("text_contrast_boost", g.textContrastBoost);
                    if (go.contains("dynamic_color") && go["dynamic_color"].is_boolean())
                        g.dynamicColor = go["dynamic_color"].get<bool>() ? 1 : 0;
                }
            }
            // Brand-mark animation style: 0 = ui::TesseractAnim::EightCell (the default),
            // 1 = DoubleRotation, 2 = Isoclinic, 3 = Octave.
            //
            // MIGRATION. Configs from the brief pre-reduction era encode 0 = SimpleRotation,
            // 1 = DoubleRotation, 2 = EightCell — both dead styles were the same bisecting-plane
            // rotation the modern 0 descends from, and the two-style build already rewrote such
            // configs to 0/1. A stale 2 that never got rewritten now lands on Isoclinic instead:
            // a one-time cosmetic swap the picker undoes in one click, accepted to keep the
            // encoding dense. Anything beyond the current Count (a newer config read by an
            // older build) falls back to the default 8-cell.
            const int ba = app.is_object() ? app.value("brand_anim", 0) : 0;
            s.brandAnim = (ba >= 0 && ba <= 3) ? ba : 0;
            // User accent pair ("appearance.accent.primary"/".secondary", 3-element arrays).
            // Absent or malformed (wrong length / non-numeric) leaves the Settings default in
            // place, so a hand-edited config can never paint the whole UI black.
            {
                auto ac = app.is_object() ? app.value("accent", nlohmann::json::object())
                                          : nlohmann::json::object();
                auto rgb = [&ac](const char* key, float dst[3]) {
                    if (!ac.is_object()) return;
                    auto it = ac.find(key);
                    if (it == ac.end() || !it->is_array() || it->size() != 3) return;
                    float v[3];
                    for (size_t i = 0; i < 3; ++i) {
                        if (!(*it)[i].is_number()) return;
                        v[i] = std::clamp((*it)[i].get<float>(), 0.0f, 1.0f);
                    }
                    dst[0] = v[0]; dst[1] = v[1]; dst[2] = v[2];
                };
                rgb("primary",   s.accentPrimary);
                rgb("secondary", s.accentSecondary);
            }
        }
        // Pinned-column vertical-collapse state.
        safely([&] { s.fmCollapsed = j.value("fm_collapsed", false); });
        safely([&] { s.copilotCollapsed = j.value("copilot_collapsed", false); });
        safely([&] {
            const float cw = j.value("copilot_width", 0.0f);
            s.copilotWidth = (cw > 0.0f && cw < 8000.0f) ? cw : 0.0f;
        });
        // Viewport / View-menu / sketch preferences ("view_prefs"; absent keys keep the defaults).
        safely([&] {
            const auto vp = j.find("view_prefs");
            if (vp == j.end() || !vp->is_object()) return;
            const auto flag = [&](const char* key, bool& dst) {
                const auto it = vp->find(key);
                if (it != vp->end() && it->is_boolean()) dst = it->get<bool>();
            };
            const auto ranged = [&](const char* key, int& dst, int lo, int hi) {
                const auto it = vp->find(key);
                if (it != vp->end() && it->is_number_integer()) {
                    const int v = it->get<int>();
                    if (v >= lo && v <= hi) dst = v;
                }
            };
            ranged("display_style", s.viewDisplayStyle, 0, 4);
            flag("triad", s.viewShowTriad);
            flag("ground_shadow", s.viewShowGroundShadow);
            flag("real_view", s.viewShowRealView);
            ranged("drag_mode", s.viewDragMode, 0, 1);
            flag("ribbon", s.panelRibbon);
            flag("feature_tree", s.panelFeatureTree);
            flag("ai_panel", s.panelAI);
            flag("viewport", s.panelViewport);
            flag("design_table", s.panelDesignTable);
            flag("cut_list", s.panelCutList);
            flag("equations", s.panelEquations);
            flag("sketch_grid", s.sketchShowGrid);
            flag("sketch_auto_relations", s.sketchAutoRelations);
        });
        // Settings-modal progressive disclosure. Absent => false => the modal opens in its
        // readable basic form, which is the whole point of the default.
        safely([&] { s.showAdvanced = j.value("show_advanced", false); });
        // Wave 3: File Explorer workspace + Recent files.
        safely([&] { s.workspaceDir = j.value("workspace_dir", std::string()); });
        s.recentFiles.clear();
        if (auto it = j.find("recent_files"); it != j.end() && it->is_array())
            for (const auto& e : *it)
                if (e.is_string()) s.recentFiles.push_back(e.get<std::string>());
        s.recentProjects.clear();
        if (auto it = j.find("recent_projects"); it != j.end() && it->is_array())
            for (const auto& e : *it)
                if (e.is_string()) s.recentProjects.push_back(e.get<std::string>());
    } catch (...) {
    }
    try {
        // Box.com project mirror. Only GUI-owned fields are modeled here; OAuth tokens in
        // box.auth stay on disk and are preserved by applyGuiOwnedSettings().
        auto box = j.value("box", nlohmann::json::object());
        if (box.is_object()) {
            s.boxEnabled = box.value("enabled", false);
            s.boxFolderId = box.value("folder_id", std::string("0"));
            s.boxMaxFileMb = box.value("max_file_mb", 512);
            auto auth = box.value("auth", nlohmann::json::object());
            if (auth.is_object()) {
                s.boxAuthMethod = auth.value("method", std::string("developer_token"));
                s.boxDeveloperToken =
                    auth.value("developer_token", std::string());
                s.boxClientId = auth.value("client_id", std::string());
                s.boxClientSecret = auth.value("client_secret", std::string());
            }
        }
#ifdef __EMSCRIPTEN__
        // Web: prefer durable workspace recents when present (legacy path; IDBFS now holds config).
        {
            std::string rerr;
            auto durable = chudware::fsBackend().loadRecents(rerr);
            if (!durable.empty()) s.recentFiles = std::move(durable);
        }
#endif
        // Chat-picker model visibility (opt-out hidden set).
        s.hiddenModels.clear();
        if (auto it = j.find("hidden_models"); it != j.end() && it->is_array())
            for (const auto& e : *it)
                if (e.is_string()) s.hiddenModels.insert(e.get<std::string>());
        s.shownLegacyHiddenModels.clear();
        if (auto it = j.find("shown_legacy_hidden_models"); it != j.end() && it->is_array())
            for (const auto& e : *it)
                if (e.is_string()) s.shownLegacyHiddenModels.insert(e.get<std::string>());
        s.disabledProviders.clear();
        if (auto it = j.find("disabled_providers"); it != j.end() && it->is_array())
            for (const auto& e : *it)
                if (e.is_string()) s.disabledProviders.insert(e.get<std::string>());
    } catch (...) {
    }
    try {
        // Experimental AI features ("experimental" block) — all default-off.
        auto exp = j.value("experimental", nlohmann::json::object());
        if (exp.is_object()) {
            auto vr = exp.value("vision_review", nlohmann::json::object());
            if (vr.is_object()) {
                s.expVisionReview = vr.value("enabled", false);
                copyStr(s.expVisionModel, sizeof(s.expVisionModel),
                        vr.value("model", std::string("qwen2.5-vl-7b-instruct")));
                copyStr(s.expVisionBaseUrl, sizeof(s.expVisionBaseUrl),
                        vr.value("base_url", std::string("http://localhost:1234/v1")));
                copyStr(s.expVisionKey, sizeof(s.expVisionKey), vr.value("api_key", std::string()));
            }
            s.expCadSequenceMode = exp.value("cad_sequence_mode", false);
        }
    } catch (...) {
    }
#ifdef __EMSCRIPTEN__
    primeRecentFilesPersistence(s.recentFiles);
#endif
}

namespace {
// Advisory exclusive lock on <config.json>.lock — deliberately the SAME file
// core/agent/auth.cpp's FileLock and services/agent/providers/sub_auth.py's _lock_path
// take, so the GUI, the engine and the Python worker serialize against one another.
// flock on POSIX, LockFileEx on Windows; a failure to acquire degrades to an unlocked
// write rather than losing the user's edit.
class ConfigFileLock {
public:
    explicit ConfigFileLock(const std::string& p) {
#if defined(_WIN32)
        h_ = CreateFileA(p.c_str(), GENERIC_READ | GENERIC_WRITE,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_ALWAYS,
                         FILE_ATTRIBUTE_NORMAL, nullptr);
        if (h_ == INVALID_HANDLE_VALUE) { h_ = nullptr; return; }
        OVERLAPPED ov{};
        if (!LockFileEx(h_, LOCKFILE_EXCLUSIVE_LOCK, 0, 1, 0, &ov)) {
            CloseHandle(h_);
            h_ = nullptr;
        }
#elif !defined(__EMSCRIPTEN__)
        fd_ = ::open(p.c_str(), O_CREAT | O_RDWR, 0600);
        if (fd_ >= 0) (void)::fchmod(fd_, 0600);
        if (fd_ < 0) return;
        if (::flock(fd_, LOCK_EX) != 0) { ::close(fd_); fd_ = -1; }
#else
        (void)p;  // single-threaded browser build: no other process shares this file
#endif
    }
    ~ConfigFileLock() {
#if defined(_WIN32)
        if (h_) { OVERLAPPED ov{}; UnlockFileEx(h_, 0, 1, 0, &ov); CloseHandle(h_); }
#elif !defined(__EMSCRIPTEN__)
        if (fd_ >= 0) { ::flock(fd_, LOCK_UN); ::close(fd_); }
#endif
    }
    ConfigFileLock(const ConfigFileLock&) = delete;
    ConfigFileLock& operator=(const ConfigFileLock&) = delete;

private:
#if defined(_WIN32)
    HANDLE h_ = nullptr;
#elif !defined(__EMSCRIPTEN__)
    int fd_ = -1;
#endif
};

std::mutex g_configWarningMutex;
// The writer has no AppState reference (and may run on a worker thread). The UI
// consumes this once on its frame thread rather than coupling config I/O to ImGui.
std::string g_configCorruptionWarning;
}  // namespace
std::string takeConfigCorruptionWarning() {
    std::lock_guard<std::mutex> guard(g_configWarningMutex);
    std::string warning;
    warning.swap(g_configCorruptionWarning);
    return warning;
}


static bool writeConfigJson(const std::function<void(nlohmann::json&)>& patch) {
    const std::string path = configPath();
    std::error_code ec;
    std::filesystem::create_directories(appdataAppDir(), ec);
#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
    (void)::chmod(appdataAppDir().c_str(), 0700);
#endif
    ConfigFileLock lock(path + ".lock");
    // Re-read INSIDE the lock: anything we parsed earlier may be stale by now.
    nlohmann::json j = nlohmann::json::object();
    bool corrupt = false;
    {
        std::ifstream in(path);
        if (in) {
            try {
                nlohmann::json old;
                in >> old;
                if (in.bad()) return false;  // I/O failure is not a parse failure.
                if (old.is_object()) j = std::move(old);
                else corrupt = true;  // a config root must be an object
            } catch (const nlohmann::json::parse_error&) {
                corrupt = true;
            } catch (...) {
                return false;
            }
        } else {
            // Missing config is a first run; an existing but unreadable one must
            // never be treated as empty and replaced by a partial settings patch.
            if (std::filesystem::exists(path, ec) || ec) return false;
        }
    }
    try {
        patch(j);
    } catch (...) {
        return false;  // a throwing patch must not leave a partial write behind
    }
    if (corrupt) {
        // Preserve unknown (possibly recoverable) credentials and other keys before
        // publishing a fresh config. Never overwrite a previous corruption backup.
        const std::string stem = path + ".corrupt-" + std::to_string(
            std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::system_clock::now().time_since_epoch()).count());
        std::string backup = stem;
        for (unsigned i = 1; std::filesystem::exists(backup, ec); ++i) {
            if (ec) break;
            backup = stem + "-" + std::to_string(i);
        }
        if (ec) {
            std::fprintf(stderr, "[chudware] cannot check config backup: %s\n", ec.message().c_str());
            return false;
        }
        std::filesystem::rename(path, backup, ec);
        if (ec) {
            std::fprintf(stderr, "[chudware] cannot back up corrupt config: %s\n", ec.message().c_str());
            return false;  // fail closed; do not overwrite the only copy
        }
        std::fprintf(stderr, "[chudware] corrupt settings file moved to %s\n", backup.c_str());
        {
            std::lock_guard<std::mutex> guard(g_configWarningMutex);
            if (g_configCorruptionWarning.empty())
                g_configCorruptionWarning = "Settings file was corrupt; backup saved at " +
                                            backup + ". Review your settings.";
        }
        appstatePersistSync();  // persist the backup itself if a subsequent write fails (IDBFS)
    }
    try {
        // Per-process tmp name: the lock already serializes well-behaved writers, but a
        // shared ".tmp" means any writer that skips the lock races us into renaming a file
        // we already moved, or publishes our half-written one.
        const std::string tmp = path + ".tmp." + std::to_string(
#if defined(_WIN32)
            (long long)GetCurrentProcessId()
#else
            (long long)::getpid()
#endif
        );
#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
        const int tmpFd = ::open(tmp.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
        if (tmpFd < 0) return false;
        const std::string data = j.dump(2);
        size_t written = 0;
        while (written < data.size()) {
            const ssize_t n = ::write(tmpFd, data.data() + written, data.size() - written);
            if (n <= 0) { ::close(tmpFd); std::filesystem::remove(tmp, ec); return false; }
            written += static_cast<size_t>(n);
        }
        if (::fsync(tmpFd) != 0 || ::close(tmpFd) != 0) {
            std::filesystem::remove(tmp, ec);
            return false;
        }
#else
        {
            std::ofstream out(tmp, std::ios::trunc);
            if (!out) return false;
            out << j.dump(2);
            if (!out) return false;
        }
#endif
        std::filesystem::rename(tmp, path, ec);
        if (ec) {
            std::filesystem::remove(tmp, ec);  // don't leave a stray tmp behind
            return false;
        }
#if !defined(_WIN32) && !defined(__EMSCRIPTEN__)
        (void)::chmod(path.c_str(), 0600);
#endif
        appstatePersistSync();  // WASM: flush IDBFS; no-op on desktop
    } catch (...) {
        return false;
    }
    return true;
}

namespace {
// Apply only the caller's changed leaves to the latest locked config. Object maps are
// merged recursively so unrelated OAuth/provider keys written by another process survive.
void mergeConfigChanges(nlohmann::json& live, const nlohmann::json& before,
                        const nlohmann::json& after) {
    if (before == after) return;
    if (!after.is_object()) {
        live = after;
        return;
    }
    if (!live.is_object()) live = nlohmann::json::object();
    if (before.is_object()) {
        for (auto it = before.begin(); it != before.end(); ++it)
            if (!after.contains(it.key())) live.erase(it.key());
    }
    for (auto it = after.begin(); it != after.end(); ++it) {
        const auto old = before.is_object() ? before.find(it.key()) : before.end();
        if (before.is_object() && old != before.end()) {
            if (*old != it.value()) mergeConfigChanges(live[it.key()], *old, it.value());
        } else {
            mergeConfigChanges(live[it.key()], nlohmann::json(), it.value());
        }
    }
}

#ifndef __EMSCRIPTEN__
class ConfigWriter {
public:
    ~ConfigWriter() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            requeueFailed();
            stopping_ = true;
        }
        cv_.notify_all();
        if (thread_.joinable()) thread_.join();
    }

    nlohmann::json read() {
        std::lock_guard<std::mutex> lock(mutex_);
        // Hold the state lock across the read so a completing rename cannot retire an
        // in-flight patch between our disk snapshot and its read-your-writes overlay.
        nlohmann::json live = readConfigFile();
        if (writing_) mergeConfigChanges(live, inFlightBefore_, inFlightAfter_);
        if (dirty_) mergeConfigChanges(live, baseline_, shadow_);
        else if (failedBaseline_) mergeConfigChanges(live, *failedBaseline_, shadow_);
        if (!writing_ && !dirty_ && !failedBaseline_) shadow_ = live;
        initialized_ = true;
        return live;
    }

    bool update(const std::function<void(nlohmann::json&)>& patch) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!initialized_ || (!dirty_ && !writing_ && !failedBaseline_)) {
            shadow_ = readConfigFile();
            initialized_ = true;
        }
        nlohmann::json next = shadow_;
        try {
            patch(next);  // consume reference captures before the caller returns
        } catch (...) {
            return false;
        }
        if (!next.is_object()) return false;
        if (next == shadow_ && !failedBaseline_) return true;
        requeueFailed();
        if (!dirty_) baseline_ = shadow_;
        shadow_ = std::move(next);
        dirty_ = baseline_ != shadow_;
        if (!dirty_) {
            flushed_.notify_all();
            return true;
        }
        if (!thread_.joinable()) thread_ = std::thread([this] { run(); });
        cv_.notify_one();
        return true;
    }

    bool flush() {
        std::unique_lock<std::mutex> lock(mutex_);
        if (!thread_.joinable() || (!dirty_ && !writing_ && !failedBaseline_)) return true;
        requeueFailed();
        cv_.notify_one();
        flushed_.wait(lock, [this] { return !dirty_ && !writing_; });
        return !failedBaseline_;
    }

private:
    void requeueFailed() {
        if (!failedBaseline_) return;
        if (!dirty_) {
            baseline_ = std::move(*failedBaseline_);
            dirty_ = true;
        }
        failedBaseline_.reset();
    }

    void run() {
        std::unique_lock<std::mutex> lock(mutex_);
        for (;;) {
            cv_.wait(lock, [this] { return stopping_ || dirty_; });
            if (stopping_ && !dirty_) return;
            inFlightBefore_ = std::move(baseline_);
            inFlightAfter_ = shadow_;
            dirty_ = false;
            writing_ = true;
            lock.unlock();
            const bool ok = writeConfigJson([&](nlohmann::json& live) {
                mergeConfigChanges(live, inFlightBefore_, inFlightAfter_);
            });
            if (!ok) {
                std::lock_guard<std::mutex> warningLock(g_configWarningMutex);
                g_configCorruptionWarning = "Settings could not be saved. Check the settings "
                                            "directory's permissions and available disk space.";
            }
            lock.lock();
            if (!ok) {
                // Keep the unsaved baseline for the next edit or shutdown flush.
                // Do not spin on an unwritable config directory.
                if (dirty_) baseline_ = std::move(inFlightBefore_);
                else failedBaseline_ = std::move(inFlightBefore_);
            }
            writing_ = false;
            flushed_.notify_all();
        }
    }

    std::mutex mutex_;
    std::condition_variable cv_, flushed_;
    std::thread thread_;
    nlohmann::json shadow_ = nlohmann::json::object();
    nlohmann::json baseline_;
    nlohmann::json inFlightBefore_, inFlightAfter_;
    std::optional<nlohmann::json> failedBaseline_;
    bool dirty_ = false, writing_ = false, stopping_ = false, initialized_ = false;
};

ConfigWriter& configWriter() {
    static ConfigWriter writer;
    return writer;
}
#endif


}  // namespace

bool updateConfigJson(const std::function<void(nlohmann::json&)>& patch) {
    if (!patch) return false;
#ifdef __EMSCRIPTEN__
    return writeConfigJson(patch);
#else
    return configWriter().update(patch);
#endif
}

nlohmann::json readConfigJson() {
#ifdef __EMSCRIPTEN__
    return readConfigFile();
#else
    return configWriter().read();
#endif
}

bool flushConfigWrites() {
#ifndef __EMSCRIPTEN__
    return configWriter().flush();
#else
    return true;
#endif
}

// j[key] as a string, or `def` when the key is absent, null or not a string. nlohmann's
// value() THROWS on a type mismatch, and a throwing patch makes ConfigWriter::update drop the
// WHOLE save — so one hand-edited/foreign null here silently stopped every setting persisting.
static std::string jsonStringOr(const nlohmann::json& j, const char* key,
                                const std::string& def = std::string()) {
    if (!j.is_object()) return def;
    const auto it = j.find(key);
    return (it != j.end() && it->is_string()) ? it->get<std::string>() : def;
}

// Overlay every GUI-owned key onto *j*, the live on-disk config. Merge-not-replace:
// config.json is SHARED with the Python side (subscription_auth OAuth tokens, plm.*,
// ai_roles, ...) — a fixed-allowlist full overwrite here silently wiped every signed-in
// account whenever any setting was saved (2026-07-14 wipe incident). The read and the
// write live in updateConfigJson, which holds the shared lock across both.
static void applyGuiOwnedSettings(nlohmann::json& j, const Settings& s) {
    // Capture partially-modeled maps BEFORE erase. config.json "keys" is shared with the
    // agent harness (openrouter, etc.); "experimental" may grow fields the GUI doesn't
    // model. Rebuild-from-struct alone wiped unknown entries on every save (same class as
    // the 2026-07-14 subscription_auth wipe). Overlay GUI-owned slots onto the on-disk map.
    nlohmann::json keysOut = nlohmann::json::object();
    if (j.contains("keys") && j["keys"].is_object()) keysOut = j["keys"];
    // A pending legacy online key is kept separate from any local endpoint credential.
    const std::string legacyApiProvider = jsonStringOr(
        j, "legacy_api_key_provider", jsonStringOr(j, "provider"));
    const auto oldEndpoint = j.value("openai", nlohmann::json::object());
    const bool oldLocalKey = !j.contains("legacy_api_key_provider") &&
        legacyApiProvider == "openai" && oldEndpoint.is_object() &&
        ui::baseUrlIsLoopback(jsonStringOr(oldEndpoint, "base_url"));
    const nlohmann::json legacyApiKey = j.contains("legacy_api_key") ? j["legacy_api_key"] :
        oldLocalKey ? nlohmann::json() : j.value("api_key", nlohmann::json());
    nlohmann::json expOut = nlohmann::json::object();
    if (j.contains("experimental") && j["experimental"].is_object()) expOut = j["experimental"];
    nlohmann::json boxOut = nlohmann::json::object();
    if (j.contains("box") && j["box"].is_object()) boxOut = j["box"];
    nlohmann::json boxAuthOut = nlohmann::json::object();
    if (boxOut.contains("auth") && boxOut["auth"].is_object()) boxAuthOut = boxOut["auth"];
    // Erase GUI-owned keys that are conditionally emitted below, so stale values never linger
    // after the user clears them or switches provider. Unknown top-level keys survive untouched.
    // "keys" / "experimental" are re-emitted via merge-overlay (not a full replace).
    for (const char* k : {"provider", "model", "api_key", "openai", "llamacpp", "freellmapi",
                          "keys", "reasoning_effort", "max_output_tokens",
                          "context_window", "system_prompt", "appearance", "workspace_dir",
                          "hidden_models", "shown_legacy_hidden_models", "disabled_providers", "experimental", "box",
                          "model_defaults_seeded"})
        j.erase(k);
    if (s.provider == 4) {
        j["provider"] = "gemini";
        j["model"] = s.model[0] ? s.model : "gemini-3.5-flash";
        j["api_key"] = s.geminiKey;
    } else if (s.provider == 0) {
        j["provider"] = "anthropic";
        j["model"] = s.model[0] ? s.model : "claude-opus-5-5";
        j["api_key"] = s.anthropicKey;
    } else if (s.provider == 1) {
        j["provider"] = "openai";
        j["model"] = s.model[0] ? s.model : "gpt-6-sol";
        j["api_key"] = s.openaiKey;
    } else if (s.provider == 2) {
        j["provider"] = "openai";
        j["model"] = s.model;
        j["api_key"] = s.apiKey[0] ? s.apiKey : "lm-studio";
        j["openai"] = {{"base_url", s.baseUrl}, {"vision", s.vision}};
    } else if (s.provider == 5) {
        // Shared cloud slot, discriminated by base_url: ui::cloudServiceKey() names the service
        // and ui::cloudServiceWireName() gives the provider name Chudcode expects — native
        // "moonshot" / generic "openai", or a hosted catalog id (openrouter, deepseek,
        // alibaba-coding-plan, amazon-bedrock, azure, google-antigravity, aimlapi). Persisting
        // the classified name keeps config.json aligned with the per-send override. Every
        // service here is a cloud endpoint, so explicit offline mode routes away from it.
        const char* svc = ui::cloudServiceKey(s.baseUrl);
        const bool isMoonshot = std::strcmp(svc, "moonshot") == 0;
        j["provider"] = ui::cloudServiceWireName(svc);
        j["model"] = s.model[0] ? s.model : (isMoonshot ? "kimi-k3" : "qwen-max");
        // Hosted services keep credentials in Chudcode's auth store. Do not attach the shared
        // cloud-slot key unless this service explicitly uses a local key.
        if (ui::cloudServiceUsesLocalKey(svc))
            j["api_key"] = isMoonshot ? s.moonshotKey : s.cloudKey;
        j["openai"] = {{"base_url", s.baseUrl[0] ? s.baseUrl
                                                 : "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
                       {"vision", s.vision}};
    } else if (s.provider == 6) {
        // xAI Grok. Prefer API-key path when xaiKey is set; otherwise wire as xai-sub so the
        // agent uses the SuperGrok / X Premium+ OAuth credential from subscription_auth.
        if (s.xaiKey[0]) {
            j["provider"] = "xai";
            j["api_key"] = s.xaiKey;
        } else {
            j["provider"] = "xai-sub";
        }
        j["model"] = s.model[0] ? s.model : "grok-4.5";
    } else if (s.provider == 7) {
        // Cursor — subscription OAuth via subscription_auth; no API key field.
        j["provider"] = "cursor";
        j["model"] = s.model[0] ? s.model : "composer-2.5";
    } else if (s.provider == 8) {
        // Kimi Code subscription — OAuth credential lives in subscription_auth (like xai-sub).
        // Distinct from Moonshot API key path (provider "moonshot" / keys.moonshot).
        j["provider"] = "kimi-sub";
        j["model"] = s.model[0] ? s.model : "k3";
    } else if (s.provider == 9) {
        // ZenMux authentication and completions are owned by Chudcode. No local api_key is
        // serialized here. Default provider: free x-ai/grok-4.5-free tier.
        j["provider"] = "zenmux";
        j["model"] = s.model[0] ? s.model : "x-ai/grok-4.5-free";
    } else if (s.provider == 10) {
        // Free AI (FreeLLMAPI) — native "freellmapi" wire, own config namespace (never shares
        // cfg["openai"] with a real local OpenAI-compatible server). Requires the unified key;
        // core/agent/providers.cpp refuses the call with a clear error when it's empty.
        j["provider"] = "freellmapi";
        j["model"] = s.model[0] ? s.model : "auto";
        j["api_key"] = s.freellmapiKey;
        j["freellmapi"] = {{"base_url", s.baseUrl[0] ? s.baseUrl : "http://localhost:3001/v1"},
                           {"vision", s.vision}};
    } else if (s.provider == 3) {
        j["provider"] = "llamacpp";
        j["model"] = "local-gguf";
        j["llamacpp"] = {
            {"base_url", s.baseUrl[0] ? s.baseUrl : "http://127.0.0.1:1234/v1"},
            {"model_path", s.model[0] ? s.model : kDefaultLlamaModelPath},
            {"auto_start", true},
            {"vision", s.vision}
        };
    } else {
        // Fallback: treat unknown provider indices as managed llama.cpp (legacy default).
        j["provider"] = "llamacpp";
        j["model"] = "local-gguf";
        j["llamacpp"] = {
            {"base_url", s.baseUrl[0] ? s.baseUrl : "http://127.0.0.1:1234/v1"},
            {"model_path", s.model[0] ? s.model : kDefaultLlamaModelPath},
            {"auto_start", true},
            {"vision", s.vision}
        };
    }
    // The online pin is independent of legacy offline provider slots.
    if (!s.ompProvider.empty()) {
        j["omp_provider"] = s.ompProvider;
        if (s.provider != 2 && s.provider != 3) {
            j["provider"] = s.ompProvider;
            j["model"] = s.model;
            j.erase("api_key");
        }
    } else {
        j.erase("omp_provider");
        if (s.provider != 2 && s.provider != 3 && s.ompProvider.empty()) {
            j.erase("provider");
            j.erase("model");
            j.erase("api_key");
        }
    }
    if (legacyApiKey.is_string() && !legacyApiKey.get_ref<const std::string&>().empty()) {
        if (s.provider == 2 || s.provider == 3 || j.contains("legacy_api_key"))
            j["legacy_api_key"] = legacyApiKey;
        else
            j["api_key"] = legacyApiKey;
        j["legacy_api_key_provider"] = legacyApiProvider;
    }
    // Persist every provider's key so switching providers never loses one, and the picker
    // can tell which providers are usable. Overlay onto the on-disk keys map so unknown
    // entries (e.g. keys.openrouter written by the agent harness) survive a load→save
    // round-trip. The active provider's key is also the top-level api_key above.
    keysOut["gemini"] = s.geminiKey;
    keysOut["anthropic"] = s.anthropicKey;
    keysOut["openai"] = s.openaiKey;
    keysOut["cloud_compat"] = s.cloudKey;
    keysOut["moonshot"] = s.moonshotKey;
    keysOut["xai"] = s.xaiKey;
    keysOut["freellmapi"] = s.freellmapiKey;
    j["keys"] = std::move(keysOut);
    // Reasoning effort applies only to reasoning-capable models; resolve_provider passes it
    // through to the provider (gemini thinkingConfig / anthropic effort).
    if (s.reasoningEffort[0]) j["reasoning_effort"] = s.reasoningEffort;
    j["composer_follow_roles"] = s.composerFollowRoles;
        j["offline_mode"] = s.offlineMode;
        j["compliance_mode"] = s.complianceMode[0] ? s.complianceMode : "off";
    {
        static const char* kPrefNames[] = {"off", "option", "encourage", "require"};
        auto prefName = [&](int p) -> const char* {
            if (p < 0 || p > 3) p = 0;
            return kPrefNames[p];
        };
        // planPreference is authoritative; planMode is the derived binary gate.
        const int planPref = (s.planPreference < 0 || s.planPreference > 3) ? 0 : s.planPreference;
        // (const Settings& — do not write back; callers keep planMode in sync on UI change)
        j["plan_preference"] = prefName(planPref);
        j["plan_mode"] = (planPref != 0);  // Python / older readers
        j["subagent_preference"] = prefName(s.subagentPreference);
        j["task_list_preference"] = prefName(s.taskListPreference);
        j["advisor_preference"] = prefName(s.advisorPreference);
        j["proactive_advisor"] = s.proactiveAdvisor;
        j.erase("corporate_mode");
        j["memory_backend"] = s.memoryBackend;
        j["memory_enabled"] = s.memoryBackend != "off";
        j["memory_token_budget"] = s.memoryTokenBudget;
        // Merge-overlay into agent{} so Python config helpers see the same values.
        nlohmann::json agent = nlohmann::json::object();
        if (j.contains("agent") && j["agent"].is_object()) agent = j["agent"];
        agent.erase("role_adoption");
        agent.erase("corporate_mode");
        agent["advisor"] = (s.advisorPreference != 0);
        agent["advisor_preference"] = prefName(s.advisorPreference);
        agent["proactive_advisor"] = s.proactiveAdvisor;
        agent["subagent_preference"] = prefName(s.subagentPreference);
        agent["task_list_preference"] = prefName(s.taskListPreference);
        agent["memory_backend"] = s.memoryBackend;
        agent["memory_enabled"] = s.memoryBackend != "off";
        agent["memory_token_budget"] = s.memoryTokenBudget;
        j["agent"] = std::move(agent);
        j.erase("role_adoption");
    }
    {
        static const char* kModeNames[kCopilotModeCount] = {"agent", "plan", "ask"};
        j["copilot_mode"] = kModeNames[normalizeCopilotMode(s.copilotMode)];
    }
    if (s.maxTokens > 0) j["max_output_tokens"] = s.maxTokens;
    if (s.contextWindow > 0) j["context_window"] = s.contextWindow;
    if (!s.systemPrompt.empty()) j["system_prompt"] = s.systemPrompt;
    j["theme"] = s.theme;
    j["orthographic"] = s.orthographic;
    j["display_quality"] = s.displayQuality;
    j["sketch_drag_snap_relations"] = s.sketchDragSnapRelations;
    j["units"] = (s.units == Units::IPS) ? "ips" : "mmgs";
    j["parts_grid_view"] = s.partsGridView;
    j["parts_sort_mode"] = s.partsSortMode;
    j["parts_expanded_categories"] = s.partsExpandedCats;
    // v2.1 colored liquid glass. Emit only fields the user moved off their sentinel and only
    // groups with at least one such field, so a default config carries no "appearance" block.
    {
        nlohmann::json glassJ = nlohmann::json::object();
        const ui::GlassStyle def;   // sentinel defaults
        for (size_t gi = 0; gi < (size_t)ui::GlassGroup::Count; ++gi) {
            const ui::GlassStyle& g = s.glassCfg.group[gi];
            nlohmann::json go = nlohmann::json::object();
            if (g.tintHue           != def.tintHue)           go["tint_hue"] = g.tintHue;
            if (g.tintSat           != def.tintSat)           go["tint_sat"] = g.tintSat;
            if (g.tintStrength      != def.tintStrength)      go["tint_strength"] = g.tintStrength;
            if (g.glassLevel        != def.glassLevel)        go["glass_level"] = g.glassLevel;
            if (g.opacityMul        != def.opacityMul)        go["opacity"] = g.opacityMul;
            if (g.rounding          != def.rounding)          go["rounding"] = g.rounding;
            if (g.textContrastBoost != def.textContrastBoost) go["text_contrast_boost"] = g.textContrastBoost;
            if (g.dynamicColor      != def.dynamicColor)      go["dynamic_color"] = (g.dynamicColor == 1);
            if (!go.empty()) glassJ[kGlassGroupKey[gi]] = go;
        }
        // "appearance" now carries several independent keys, so build the object incrementally:
        // any may be absent, and a wholly default config still writes no block at all.
        nlohmann::json appJ = nlohmann::json::object();
        if (!glassJ.empty()) appJ["glass"] = glassJ;
        if (s.brandAnim != 0) appJ["brand_anim"] = s.brandAnim;
        // Accent pair: same omit-when-default rule as the glass sentinels. Exact compare against
        // the shipped defaults, not an epsilon — the picker writes whatever ColorEdit3 produced,
        // and "Reset to defaults" copies the constants back byte-for-byte.
        {
            nlohmann::json accentJ = nlohmann::json::object();
            auto emitIfMoved = [&accentJ](const char* key, const float v[3], const float def[3]) {
                if (v[0] == def[0] && v[1] == def[1] && v[2] == def[2]) return;
                accentJ[key] = {v[0], v[1], v[2]};
            };
            emitIfMoved("primary",   s.accentPrimary,   ui::kDefaultAccentPrimary);
            emitIfMoved("secondary", s.accentSecondary, ui::kDefaultAccentSecondary);
            if (!accentJ.empty()) appJ["accent"] = accentJ;
        }
        if (!appJ.empty()) j["appearance"] = appJ;
    }
    // Pinned-column vertical-collapse state.
    j["fm_collapsed"] = s.fmCollapsed;
    j["copilot_collapsed"] = s.copilotCollapsed;
    if (s.copilotWidth > 0.0f) j["copilot_width"] = s.copilotWidth;
    else j.erase("copilot_width");
    {
        // Only keys that differ from their default are written, so an untouched config carries no
        // "view_prefs" block at all.
        nlohmann::json v = nlohmann::json::object();
        if (s.viewDisplayStyle != 1) v["display_style"] = s.viewDisplayStyle;
        if (!s.viewShowTriad) v["triad"] = false;
        if (!s.viewShowGroundShadow) v["ground_shadow"] = false;
        if (!s.viewShowRealView) v["real_view"] = false;
        if (s.viewDragMode != 0) v["drag_mode"] = s.viewDragMode;
        if (!s.panelRibbon) v["ribbon"] = false;
        if (!s.panelFeatureTree) v["feature_tree"] = false;
        if (!s.panelAI) v["ai_panel"] = false;
        if (!s.panelViewport) v["viewport"] = false;
        if (s.panelDesignTable) v["design_table"] = true;
        if (s.panelCutList) v["cut_list"] = true;
        if (s.panelEquations) v["equations"] = true;
        if (s.sketchShowGrid) v["sketch_grid"] = true;
        if (!s.sketchAutoRelations) v["sketch_auto_relations"] = false;
        if (v.empty()) j.erase("view_prefs");
        else j["view_prefs"] = std::move(v);
    }
    // Settings-modal progressive disclosure. Omitted while false so a default config
    // carries no trace of it.
    if (s.showAdvanced) j["show_advanced"] = true;
    else j.erase("show_advanced");  // OFF must reach disk: the writer patches the on-disk shadow
    // Wave 3: File Explorer workspace + Recent files.
    if (!s.workspaceDir.empty()) j["workspace_dir"] = s.workspaceDir;
    j["recent_files"] = s.recentFiles;
    j["recent_projects"] = s.recentProjects;
    // Box.com mirror. Merge-overlay both levels so Python-owned OAuth tokens and unknown
    // future fields survive every GUI settings write.
    boxOut["enabled"] = s.boxEnabled;
    boxOut["folder_id"] = s.boxFolderId.empty() ? "0" : s.boxFolderId;
    boxOut["max_file_mb"] = s.boxMaxFileMb;
    boxAuthOut["method"] =
        s.boxAuthMethod == "oauth" ? "oauth" : "developer_token";
    boxAuthOut["developer_token"] = s.boxDeveloperToken;
    boxAuthOut["client_id"] = s.boxClientId;
    boxAuthOut["client_secret"] = s.boxClientSecret;
    boxOut["auth"] = std::move(boxAuthOut);
    j["box"] = std::move(boxOut);
    if (!s.hiddenModels.empty())
        j["hidden_models"] = std::vector<std::string>(s.hiddenModels.begin(), s.hiddenModels.end());
    if (!s.shownLegacyHiddenModels.empty())
        j["shown_legacy_hidden_models"] = std::vector<std::string>(s.shownLegacyHiddenModels.begin(), s.shownLegacyHiddenModels.end());
    if (!s.disabledProviders.empty())
        j["disabled_providers"] =
            std::vector<std::string>(s.disabledProviders.begin(), s.disabledProviders.end());
    // Experimental AI features. Merge-overlay: keep unknown fields in the on-disk
    // experimental block; only rewrite/clear the slots the GUI models. Omit the whole
    // block when nothing remains so a default config stays clean.
    // Disabled does not forget: a customized model/endpoint/key stays on disk with
    // "enabled": false (resolve_review_provider treats that as off) so re-enabling restores the
    // user's choice. A never-customized block is omitted so a default config stays clean.
    {
        const std::string vModel = s.expVisionModel[0] ? s.expVisionModel : "qwen2.5-vl-7b-instruct";
        const std::string vBase = s.expVisionBaseUrl[0] ? s.expVisionBaseUrl
                                                        : "http://localhost:1234/v1";
        const bool customized = vModel != "qwen2.5-vl-7b-instruct" ||
                                vBase != "http://localhost:1234/v1" || s.expVisionKey[0];
        if (s.expVisionReview || customized) {
            expOut["vision_review"] = {
                {"enabled", s.expVisionReview},
                {"model", vModel},
                {"base_url", vBase},
                {"api_key", s.expVisionKey},
            };
        } else {
            expOut.erase("vision_review");
        }
    }
    if (s.expCadSequenceMode) expOut["cad_sequence_mode"] = true;
    else expOut.erase("cad_sequence_mode");
    if (!expOut.empty()) j["experimental"] = std::move(expOut);
}

void saveSettings(const Settings& s) {
    // The updater consumes this patch now and coalesces its owned JSON changes; disk
    // locking, serialization and fsync run only on its writer thread.
    if (!updateConfigJson([&s](nlohmann::json& j) { applyGuiOwnedSettings(j, s); })) {
        // The patch was rejected or the write failed outright (it threw on a malformed config
        // value / the web store is unwritable): nothing was queued. Say so once instead of
        // letting every later edit vanish silently.
        static bool s_reported = false;
        if (!s_reported) {
            s_reported = true;
            std::lock_guard<std::mutex> guard(g_configWarningMutex);
            g_configCorruptionWarning =
                "Settings could not be saved. Check config.json (a malformed value) and the "
                "settings directory's permissions and free space.";
        }
    }
#ifdef __EMSCRIPTEN__
    // The workspace endpoint is independent of config.json/IDBFS. Only an actual recents
    // mutation should reach it; theme/provider and other settings saves stay local.
    persistRecentFilesIfChanged(s.recentFiles);
#endif
}

// ---------------- textures ----------------
Tex loadTexture(const std::string& path) {
    Tex t;
    int n;
    unsigned char* d = stbi_load(path.c_str(), &t.w, &t.h, &n, 4);
    if (!d) return t;
    glGenTextures(1, &t.id);
    glBindTexture(GL_TEXTURE_2D, t.id);
    // Mipmapped minification. The brand wordmark is a 687x200 PNG drawn ~14 px tall in the
    // title bar; plain GL_LINEAR minification samples 4 of ~14 source texels per output pixel
    // and shredded the type into the blocky mark the title bar shipped with. A mip chain makes
    // that a properly filtered box-average instead. Magnification stays GL_LINEAR.
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, t.w, t.h, 0, GL_RGBA, GL_UNSIGNED_BYTE, d);
    glGenerateMipmap(GL_TEXTURE_2D);
    stbi_image_free(d);
    return t;
}
void setWindowIcon(GLFWwindow* win, const std::string& path) {
    int w, h, n;
    unsigned char* src = stbi_load(path.c_str(), &w, &h, &n, 4);
    if (!src) return;

    static const int kSizes[] = {16, 32, 48, 64, 128, 256};
    static const int kCount   = (int)(sizeof(kSizes) / sizeof(kSizes[0]));

    // assets/appicon/<N>.png — the brand mark rendered NATIVELY at each size (assets/make_logo.py
    // make_icon_set). The mark is a wireframe tesseract: resampling the 256 master down to 16/24
    // smears its 32 edges into a patch, whereas the native render picks draw_mark's reduced
    // small-size treatment and stays legible. Fall back to resampling `path` per size when the
    // set is absent (older/partial installs), so this never regresses to a missing icon.
    const std::string dir = std::filesystem::path(path).parent_path().string();

    GLFWimage imgs[kCount];
    unsigned char* bufs[kCount] = {};
    int nImgs = 0;

    for (int i = 0; i < kCount; ++i) {
        const int s = kSizes[i];
        int nw = 0, nh = 0, nc = 0;
        unsigned char* native =
            stbi_load((dir + "/appicon/" + std::to_string(s) + ".png").c_str(),
                      &nw, &nh, &nc, 4);
        if (native && nw == s && nh == s) {
            imgs[nImgs] = {s, s, native};
            bufs[nImgs] = native;   // stbi buffer; freed with free() like the malloc'd ones
            ++nImgs;
            continue;
        }
        if (native) stbi_image_free(native);
        if (s > w || s > h) continue;   // don't upscale
        unsigned char* buf = (unsigned char*)malloc(s * s * 4);
        if (!buf) continue;
        stbir_resize_uint8_srgb(src, w, h, 0, buf, s, s, 0, STBIR_RGBA);
        imgs[nImgs] = {s, s, buf};
        bufs[nImgs] = buf;
        ++nImgs;
    }

    // Always include the original size if we ended up with nothing
    if (nImgs == 0) {
        imgs[0]  = {w, h, src};
        glfwSetWindowIcon(win, 1, imgs);
    } else {
        glfwSetWindowIcon(win, nImgs, imgs);
        for (int i = 0; i < nImgs; ++i) free(bufs[i]);
    }

    stbi_image_free(src);
}

// The human-readable cause of a Python traceback.
std::string errSummary(const std::string& e) {
    std::string lastExc, firstLine;
    size_t start = 0;
    while (start <= e.size()) {
        size_t nl = e.find('\n', start);
        std::string ln = e.substr(start, (nl == std::string::npos ? e.size() : nl) - start);
        while (!ln.empty() && (ln.back() == '\r' || ln.back() == ' ')) ln.pop_back();
        if (firstLine.empty() && !ln.empty()) firstLine = ln;
        if (!ln.empty() && !std::isspace((unsigned char)ln[0]) &&
            ln.rfind("HINT", 0) != 0 && ln.rfind("Traceback", 0) != 0 &&
            (ln.find("Error") != std::string::npos || ln.find("Exception") != std::string::npos))
            lastExc = ln;
        if (nl == std::string::npos) break;
        start = nl + 1;
    }
    std::string s = !lastExc.empty() ? lastExc : firstLine;
    if (s.size() > 180) s = s.substr(0, 180) + "...";
    return s;
}

std::vector<PartInfo> parseParts(const nlohmann::json& arr) {
    std::vector<PartInfo> out;
    if (!arr.is_array()) return out;
    for (auto& p : arr) {
        if (!p.is_object()) continue;
        PartInfo pi;
        pi.name = jstr(p, "name");
        pi.stlPath = jstr(p, "stl_path");
        pi.volume = jnum(p, "volume");
        auto bb = p.value("bbox", nlohmann::json::object());
        if (bb.is_object() && bb.contains("size") && bb["size"].is_array() && bb["size"].size() == 3) {
            char buf[96];
            std::snprintf(buf, sizeof(buf), "%.1f x %.1f x %.1f mm", bb["size"][0].get<double>(),
                          bb["size"][1].get<double>(), bb["size"][2].get<double>());
            pi.dims = buf;
        }
        out.push_back(std::move(pi));
    }
    return out;
}

FitReport parseFit(const nlohmann::json& f) {
    FitReport r;
    if (!f.is_object()) return r;
    r.present = true;
    r.ok = f.value("ok", false) && f.value("complete", true);
    r.summary = jstr(f, "summary");
    auto pairs = [](const nlohmann::json& arr, const char* valKey) {
        std::vector<FitPair> v;
        if (arr.is_array())
            for (auto& e : arr) {
                if (!e.is_object()) continue;
                FitPair fp;
                fp.a = jstr(e, "a");
                fp.b = jstr(e, "b");
                if (e.contains("distance_lower_bound_mm") && e["distance_lower_bound_mm"].is_number())
                    fp.distanceLowerBoundMm = e["distance_lower_bound_mm"].get<double>();
                else if (e.contains(valKey) && e[valKey].is_number()) {
                    if (e.value("approx", false))
                        fp.distanceLowerBoundMm = e[valKey].get<double>();
                    else
                        fp.value = e[valKey].get<double>();
                }
                v.push_back(std::move(fp));
            }
        return v;
    };
    if (f.contains("interferences")) r.interferences = pairs(f["interferences"], "volume");
    if (f.contains("clearances")) r.clearances = pairs(f["clearances"], "distance");
    return r;
}

PlanInfo parsePlan(const nlohmann::json& p) {
    PlanInfo pi;
    if (!p.is_object()) return pi;
    pi.present = true;
    pi.name = jstr(p, "name");
    pi.summary = jstr(p, "summary");
    pi.fit = jstr(p, "fit");
    if (auto it = p.find("parts"); it != p.end() && it->is_array())
        for (auto& q : *it) {
            if (!q.is_object()) continue;
            PlanPart pp;
            pp.name = jstr(q, "name");
            pp.purpose = jstr(q, "purpose");
            pp.dims = jstr(q, "dimensions");
            if (auto f = q.find("features"); f != q.end() && f->is_array())
                for (auto& s : *f)
                    if (s.is_string()) pp.features.push_back(s.get<std::string>());
            pi.parts.push_back(std::move(pp));
        }
    return pi;
}

// ---------------- job/run registry (per-conversation JobState slots) ----------------
// See core/app_state.h for the contract. JobState is non-copyable — every slot is created
// once (default-constructed shared_ptr<JobState>) and mutated in place for the conversation's
// entire lifetime, so a run launched against it keeps a valid, stable target even if `runs`
// rehashes on a later insert.
std::shared_ptr<JobState> AppState::runFor(const std::string& convId) {
    auto& slot = runs[convId];        // unordered_map::operator[] default-constructs on miss
    if (!slot) slot = std::make_shared<JobState>();
    return slot;
}

std::shared_ptr<JobState> AppState::activeRun() { return runFor(activeConvId()); }

JobState* AppState::runIfExists(const std::string& convId) {
    auto it = runs.find(convId);
    return (it == runs.end() || !it->second) ? nullptr : it->second.get();
}

bool AppState::anyRunActive() const {
    for (const auto& kv : runs)
        if (kv.second && kv.second->running.load()) return true;
    return false;
}

bool AppState::anyEditingRunActive() const {
    for (const auto& kv : runs)
        if (kv.second && kv.second->running.load() && !kv.second->isCompaction) return true;
    return false;
}

bool AppState::convRunning(const std::string& convId) const {
    auto it = runs.find(convId);
    return it != runs.end() && it->second && it->second->running.load();
}


std::string AppState::activeConvId() const {
    if (activeConv < 0 || activeConv >= (int)conversations.size()) return std::string();
    return conversations[activeConv].id;
}

// ---------------- AppState methods (formerly run_gui lambdas) ----------------
void AppState::addLog(const std::string& s) {
    size_t lineCount = 0;
    char last = 0;
    for (const char* p = s.c_str(); *p; ++p) {
        if (*p == '\r') continue;
        if (*p == '\n') ++lineCount;
        last = *p;
    }
    if (last != '\n' || lineCount == 0) ++lineCount;
    {
        std::lock_guard<std::mutex> lk(logMtx);
        const size_t slot = (logHead + logSize) % kLogCapacity;
        log[slot] = s;
        logLineCounts[slot] = lineCount;
        if (logSize < kLogCapacity) ++logSize;
        else logHead = (logHead + 1) % kLogCapacity;
        ++logGeneration;
    }
    // C40: tee to a file when CHUDWARE_LOG_FILE is set — the in-app console is hard to reach
    // when diagnosing interaction bugs (and stdout is lost for a windowed exe).
    static FILE* tee = [] {
        const char* p = std::getenv("CHUDWARE_LOG_FILE");
        return (p && *p) ? std::fopen(p, "a") : nullptr;
    }();
    if (tee) { std::fputs(s.c_str(), tee); std::fputc('\n', tee); std::fflush(tee); }
}

// Wave 3: record `path` as the most-recent file. De-dups (case-insensitive on Windows), moves an
// existing entry to the front, caps at kRecentFilesMax, mirrors into settings, and persists to
// config.json. A no-op for an empty path.
void AppState::pushRecentFile(const std::string& path) {
    if (path.empty()) return;
    auto sameFile = [](const std::string& a, const std::string& b) {
        if (a.size() != b.size()) return false;
        for (size_t i = 0; i < a.size(); ++i) {
            char ca = a[i], cb = b[i];
            if (ca == '/') ca = '\\';
            if (cb == '/') cb = '\\';
            if (std::tolower((unsigned char)ca) != std::tolower((unsigned char)cb)) return false;
        }
        return true;
    };
    recentFiles.erase(std::remove_if(recentFiles.begin(), recentFiles.end(),
                                     [&](const std::string& e) { return sameFile(e, path); }),
                      recentFiles.end());
    recentFiles.insert(recentFiles.begin(), path);
    if (recentFiles.size() > kRecentFilesMax) recentFiles.resize(kRecentFilesMax);
    settings.recentFiles = recentFiles;   // mirror so saveSettings serializes the live list
    saveSettings(settings);
}

void AppState::pushRecentProject(const std::string& path) {
    if (path.empty()) return;
    auto samePath = [](const std::string& a, const std::string& b) {
        if (a.size() != b.size()) return false;
        for (size_t i = 0; i < a.size(); ++i) {
            char ca = a[i], cb = b[i];
            if (ca == '/') ca = '\\';
            if (cb == '/') cb = '\\';
            if (std::tolower((unsigned char)ca) != std::tolower((unsigned char)cb))
                return false;
        }
        return true;
    };
    recentProjects.erase(
        std::remove_if(recentProjects.begin(), recentProjects.end(),
                       [&](const std::string& entry) { return samePath(entry, path); }),
        recentProjects.end());
    recentProjects.insert(recentProjects.begin(), path);
    if (recentProjects.size() > kRecentProjectsMax)
        recentProjects.resize(kRecentProjectsMax);
    settings.recentProjects = recentProjects;
    saveSettings(settings);
}

void AppState::seedViewPrefsFromSettings() {
    displayStyle = settings.viewDisplayStyle;
    showTriad = settings.viewShowTriad;
    showGroundShadow = settings.viewShowGroundShadow;
    showRealView = settings.viewShowRealView;
    vpDragMode = settings.viewDragMode;
    showRibbon = settings.panelRibbon;
    showFeatureTree = settings.panelFeatureTree;
    showAIPanel = settings.panelAI;
    showViewport = settings.panelViewport;
    showDesignTablePanel = settings.panelDesignTable;
    showCutListPanel = settings.panelCutList;
    showEquations = settings.panelEquations;
}

void AppState::persistViewPrefs() {
    if (shot) return;  // scenario runs set these for deterministic captures — never persist them
    bool dirty = false;
    const auto sync = [&dirty](auto& dst, const auto& src) {
        if (dst != src) {
            dst = src;
            dirty = true;
        }
    };
    sync(settings.viewDisplayStyle, displayStyle);
    sync(settings.viewShowTriad, showTriad);
    sync(settings.viewShowGroundShadow, showGroundShadow);
    sync(settings.viewShowRealView, showRealView);
    sync(settings.viewDragMode, vpDragMode);
    sync(settings.panelDesignTable, showDesignTablePanel);
    sync(settings.panelCutList, showCutListPanel);
    sync(settings.panelEquations, showEquations);
    if (dirty) saveSettings(settings);
}

// Canonical text for an engine-transport outage. Every worker RPC that fails while the daemon
// is down reports its own method ("Worker error (skills_list): not connected to hostd ..."),
// and the app's error sink dedupes on method+error for 60s — so ONE dead socket produced one
// toast PER METHOD. Observed 2026-08-27: simultaneous cards for unrelated startup probes plus
// a direct "New part failed" from the doc-tabs call site.
//
// The method name is deliberately dropped. Which RPC happened to be in flight is not the
// user's problem and is not actionable; the daemon being down is. Method-level detail stays in
// the log, where a diagnosis actually starts.
static constexpr const char* kDaemonOutageToast =
    "Engine daemon unavailable \xe2\x80\x94 requests are failing; retrying";

// Matches the transport-failure signature only. A real per-RPC error (bad argument, missing
// document, engine exception) must keep its own message and its own card.
static bool isDaemonTransportFailure(const std::string& m) {
    return m.find("not connected to hostd") != std::string::npos ||
           m.find("engine daemon unreachable") != std::string::npos;
}

// W1-B: post a transient toast. born is filled by drawToasts on the first frame it's seen (we can't
// reach ImGui's clock from here cheaply), so we leave born=0 and let drawToasts stamp it.
// C39: thread-safe — the worker errorSink calls this from background threads.
void AppState::toast(ui::ToastLevel level, std::string msg) {
    if (suppressToasts) return;
    // Collapse a transport outage into a single card BEFORE queueing. drawToasts already
    // collapses identical *consecutive* toasts, but these arrive with different text and
    // interleaved with unrelated toasts, so neither condition holds.
    const bool outage = isDaemonTransportFailure(msg);
    if (outage) {
        level = ui::ToastLevel::Error;
        msg.assign(kDaemonOutageToast);
    }
    ui::Toast t;
    t.level = level;
    t.msg = std::move(msg);
    t.born = 0.0;  // 0 = unstamped; drawToasts stamps it with ImGui::GetTime() on first sight
    t.ttl = ui::toastTtl(level);
    std::lock_guard<std::mutex> lk(toastsMtx);
    if (outage) {
        for (auto& q : toasts) {
            // Toasts carrying an action are never merged — they own a reversible affordance.
            if (!q.action && q.msg == kDaemonOutageToast) {
                ++q.count;
                // Re-stamp so the card's lifetime refreshes while the outage continues instead
                // of expiring mid-storm and re-appearing as a "new" error.
                q.born = 0.0;
                return;
            }
        }
    }
    toasts.push_back(std::move(t));
    // Cap the backing queue so an error storm can't grow it without bound (drawToasts shows <= 5).
    if (toasts.size() > 32) toasts.erase(toasts.begin(), toasts.begin() + (toasts.size() - 32));
}

// W1-B (item 4): shared undo/redo. Both the tree toolbar and the global keys call these.
// Multi-document: undo stacks are per-doc and shared across clients — always target the document
// the user is CURRENTLY viewing (active tab). Drawing/ECAD tabs leave DocMirror pointed at the
// previous part/asm; async tab switches update activeTab before the mirror rebinds. Refuse those
// paths so we never doc_undo/doc_redo a hidden or stale doc_id.
bool AppState::isActiveMirrorDoc() const {
    if (activeTab < 0 || activeTab >= (int)docTabs.size()) return false;
    const auto& t = docTabs[activeTab];
    if (t.kind != DocTab::TabKind::Part && t.kind != DocTab::TabKind::Assembly) return false;
    return !t.docId.empty() && docMirror.hasDoc() && t.docId == docMirror.docId();
}

core::OpsResponse AppState::docUndo() {
    if (!isActiveMirrorDoc()) {
        core::OpsResponse r;
        r.error_code = "no_doc";
        r.error_message = "no active CAD document";
        return r;
    }
    core::OpsResponse r = docMirror.undo(worker);
    if (r.ok) {
        addLog("[doc] undo  rev=" + std::to_string(r.revision));
        if (!r.aicm_path.empty()) { std::string le; loadDocMesh(r.aicm_path, le); }
        // C38 (T1): the owner map was just rebuilt; re-derive the feature highlight so the tinted
        // faces track the retagged topology instead of pointing at stale face ids.
        ui::refreshFeatureHighlight(*this);
        toast(ui::ToastLevel::Info, "Undo: ok");
    } else {
        // doc_undo with an empty stack reports a benign error code; treat it as "nothing to undo".
        bool nothing = r.error_code == "nothing_to_undo" || r.error_code == "no_history" ||
                       r.error_code == "empty_stack";
        addLog("[doc] undo: " + (r.error_message.empty() ? r.error_code : r.error_message));
        toast(nothing ? ui::ToastLevel::Info : ui::ToastLevel::Warn,
              nothing ? "Undo: nothing to undo"
                      : ("Undo failed: " + (r.error_message.empty() ? r.error_code : r.error_message)));
    }
    return r;
}

core::OpsResponse AppState::docRedo() {
    if (!isActiveMirrorDoc()) {
        core::OpsResponse r;
        r.error_code = "no_doc";
        r.error_message = "no active CAD document";
        return r;
    }
    core::OpsResponse r = docMirror.redo(worker);
    if (r.ok) {
        addLog("[doc] redo  rev=" + std::to_string(r.revision));
        if (!r.aicm_path.empty()) { std::string le; loadDocMesh(r.aicm_path, le); }
        // C38 (T1): re-derive the feature highlight after the rebuild (see docUndo).
        ui::refreshFeatureHighlight(*this);
        toast(ui::ToastLevel::Info, "Redo: ok");
    } else {
        bool nothing = r.error_code == "nothing_to_redo" || r.error_code == "no_history" ||
                       r.error_code == "empty_stack";
        addLog("[doc] redo: " + (r.error_message.empty() ? r.error_code : r.error_message));
        toast(nothing ? ui::ToastLevel::Info : ui::ToastLevel::Warn,
              nothing ? "Redo: nothing to redo"
                      : ("Redo failed: " + (r.error_message.empty() ? r.error_code : r.error_message)));
    }
    return r;
}

// Provision (force=false) or repair (force=true) the runtime on a background thread.
void AppState::startSetup(bool force) {
    // Startup owns the process/pipe lifecycle and may still publish a new child.
    // UI repair controls are disabled too; programmatic repair must obey the same gate.
    if (workerBootPending) return;
    if (setupRunning.exchange(true)) return;
    if (force) {
        // A repair rebuilds the venv from scratch (rmtree + recreate). The running worker
        // holds venv\Scripts\python.exe open, which on Windows blocks deletion and makes
        // provisioning fail (the rc=1 "repair failed" case). Stop it first; a successful
        // setup restarts it via tryStartWorker().
        worker.stop();
        workerStarted = false;
    }
    setupDone = false;
    status = force ? "Repairing runtime..." : "Setting up runtime...";
    statusKind = 0;
    addLog(force ? "[..] repairing runtime..."
                 : "[..] provisioning runtime (first run can take a minute)...");
    std::thread([this, force]() {
        std::string lg;
        bool ok = force ? repairRuntime(lg) : ensureRuntime(lg);
        std::lock_guard<std::mutex> lk(setupMtx);
        setupMsg = lg;
        setupOk = ok;
        setupDone = true;
        setupRunning = false;
    }).detach();
}

// Start the kernel worker; if it can't start, self-heal once instead of dead-ending.
void AppState::tryStartWorker() {
#ifdef __EMSCRIPTEN__
    // Browser client: the shared chudware-hostd (daemon mode) is the ONLY backend — there is no
    // Python worker to spawn and no in-process libchudware to dlopen. The ~50 workerStarted gates
    // (FeatureManager toolbar / doc ops / panels) must pass so the UI is live; engine::call
    // routes to the daemon over the WS bridge with reconnect backoff (engine_events tick).
    workerStarted = true;
    status = "Ready.";
    statusKind = 0;
    addLog("[ok] daemon mode (web): shared chudware-hostd is the backend");
#else
    if (setupRunning.load()) return;
    if (workerBoot.valid()) return;
    workerBootPending = true;
    workerBoot = std::async(std::launch::async, [this] {
        WorkerBootResult result;
        result.started = worker.start(result.workerError);
        if (!result.started) {
            result.daemonBackend = engine::daemonMode();
            if (!result.daemonBackend)
                result.coreLoaded = core::cpp_core().ensureLoaded(result.coreError);
        }
        ui::anim::wakeLoop();
        return result;
    });
#endif
}

void AppState::waitWorkerStartup() {
#ifndef __EMSCRIPTEN__
    if (workerBoot.valid()) workerBoot.wait();
#endif
}

bool AppState::pollWorkerStartup() {
#ifdef __EMSCRIPTEN__
    if (!workerBootPending || !workerStarted) return false;
    workerBootPending = false;
    return true;
#else
    if (!workerBoot.valid() ||
        workerBoot.wait_for(std::chrono::seconds(0)) != std::future_status::ready)
        return false;
    auto result = workerBoot.get();
    workerBootPending = false;
    workerStarted = result.started;
    werr = std::move(result.workerError);
    if (workerStarted) {
        status = "Ready.";
        statusKind = 0;
        addLog("[ok] kernel worker ready");
        return true;
    }
    if (result.daemonBackend) {
        // The shared daemon already won the backend verdict. Do not dlopen a
        // second, unused OCCT core: its loader lock can stall unrelated GUI
        // thread creation even though this boot itself runs off-thread.
        workerStarted = true;
        status = "Ready.";
        statusKind = 0;
        addLog("[ok] shared engine backend selected (managed worker unavailable)");
        return true;
    }
    addLog("[err] worker: " + werr);
    // If the managed worker is unavailable, the in-process C++ core is the
    // cross-platform fallback (doc/agent/sim ops route through cpp_core). This
    // is also what makes a clean Windows CI checkout able to run headless shots
    // without provisioning the Python runtime first.
    {
        if (result.coreLoaded) {
            // Treat the in-process C++ core as the live backend: the ~50 `workerStarted` gates
            // across the UI (FeatureManager toolbar / doc ops / panels) must pass, since RPCs
            // route to libchudware via worker_client. (worker_client's own `running_` stays false,
            // so methods libchudware can't serve still fail cleanly rather than hit a dead pipe.)
            workerStarted = true;
            status = "Ready.";
            statusKind = 0;
            const std::string path = core::cpp_core().loadedPath();
            const std::string bid = core::cpp_core().buildId();
            addLog("[ok] in-process C++ core (libchudware) ready path=" +
                   (path.empty() ? std::string("?") : path) +
                   " abi=" + core::cpp_core().abiVersion() +
                   (bid.empty() ? std::string() : (" build_id=" + bid)));
            return true;
        }
        addLog("[..] libchudware not loaded: " + result.coreError);
    }
    if (!autoSetupTried && !setupRunning.load()) {
        autoSetupTried = true;
        addLog("[..] attempting automatic runtime setup...");
        startSetup(runtimeReady());  // force a rebuild if a broken venv is present
    } else {
        status = "Runtime needs setup";
        statusKind = 2;
    }
    return true;
#endif
}

void AppState::clearSelection() {
    // 1. Viewport topology picks + hover/status.
    selection.clear();
    hover = {};
    pickStatus.clear();
    // 2. FeatureManager primary selection + face tint + one-shot scroll.
    selectedFeatureId.clear();
    featureHighlightFaces.clear();
    treeScrollToSelection = false;
    // 3. FeatureManager multi-selection (file-static in feature_tree.cpp).
    ui::featureClearTreeMultiSel();
    // 4. Reference-plane emphasis (viewport/FM plane selection highlight).
    planeEmphasis = -1;
    // 5. In-sketch entity + relation selection (no-op when not in a sketch).
    if (sketchMode && sketchMode->active()) sketchMode->clearEntitySelection();
}

void AppState::showPartMesh(int sel) {
    std::string e;
    if (sel >= 0 && sel < (int)asmParts.size()) {
        // isolate one body, tinted with its assembly color
        Mesh m;
        if (!loadBinarySTL(asmParts[sel].stlPath, m, e)) {
            addLog("[err] part mesh: " + e);
            return;
        }
        Renderer::PartMesh pm;
        pm.mesh = std::move(m);
        ImVec4 c = partColor(sel);
        pm.color[0] = c.x; pm.color[1] = c.y; pm.color[2] = c.z;
        renderer->setMeshes({pm});
        return;
    }
    // Whole model. Assemblies whose parts have their own STLs render each body in its
    // palette color (matching the contact sheet); otherwise fall back to the combined
    // mesh in the neutral material.
    bool distinct = false;
    for (auto& p : asmParts)
        if (!p.stlPath.empty() && p.stlPath != asmStl) { distinct = true; break; }
    if (asmParts.size() > 1 && distinct) {
        std::vector<Renderer::PartMesh> pms;
        for (size_t i = 0; i < asmParts.size(); ++i) {
            Mesh m;
            if (!loadBinarySTL(asmParts[i].stlPath, m, e)) continue;
            Renderer::PartMesh pm;
            pm.mesh = std::move(m);
            ImVec4 c = partColor((int)i);
            pm.color[0] = c.x; pm.color[1] = c.y; pm.color[2] = c.z;
            pms.push_back(std::move(pm));
        }
        if (!pms.empty()) { renderer->setMeshes(pms); return; }
    }
    if (asmStl.empty()) return;
    Mesh m;
    if (loadBinarySTL(asmStl, m, e)) renderer->setMesh(m);
    else addLog("[err] part mesh: " + e);
}

static std::vector<Renderer::PartMesh> rendererPartsFromDoc(
    const AppState& st, render::DocMesh& doc, bool includeFields = true) {
    std::vector<Renderer::PartMesh> parts;
    parts.reserve(doc.bodies.size());
    for (auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        Renderer::PartMesh pm;
        pm.mesh = std::move(b.shadedMesh);
        pm.color[0] = b.color[0];
        pm.color[1] = b.color[1];
        pm.color[2] = b.color[2];
        pm.metallic = b.metallic;
        pm.roughness = b.roughness;
        // Cached document consumers (thumbnails and motion) still need these regions.
        pm.appearance_regions = b.appearance_regions;
        pm.body_id = b.body_id;
        pm.faces.reserve(b.faces.size());
        for (const auto& fr : b.faces)
            pm.faces.push_back({fr.face_id, fr.first_tri, fr.tri_count});
        if (!includeFields) {
            parts.push_back(std::move(pm));
            continue;
        }
        render::ScalarField* chosen = nullptr;
        for (auto& sc : doc.scalars) {
            if (sc.body_id != b.body_id || sc.valueCount != b.n_verts) continue;
            if (sc.name == st.activeScalarField) {
                chosen = &sc;
                break;
            }
            if (!chosen) chosen = &sc;
        }
        if (chosen) {
            pm.scalar = std::move(chosen->values);
            pm.scalarMin = chosen->vmin;
            pm.scalarMax = chosen->vmax;
        }
        for (auto& df : doc.displacements) {
            if (df.body_id == b.body_id && df.vectorSize == (size_t)b.n_verts * 3) {
                pm.disp = std::move(df.vec);
                pm.dispScaleHint = df.scale_hint;
                break;
            }
        }
        parts.push_back(std::move(pm));
    }
    return parts;
}

// Drop id-pass / overlay GPU buffers when the viewport has no document mesh (empty doc, tab
// close, or a rebuild that produced zero bodies). Requires a current GL context.
static void clearPickBuffers(render::IdPass* idPass, render::Overlays* overlays) {
    render::DocMesh empty;
    std::string discard;
    if (idPass) {
        idPass->setDoc(empty, discard);
        idPass->setHiddenBodies({});
    }
    if (overlays) overlays->setDoc(empty, discard);
}

// Dedicated mesh-loader thread. Disk parse of AICM is the multi-100ms hit on big
// docs; it runs here. GL upload + AppState cache commit stay on the GL thread in
// drainMeshLoads / commitDocMeshFromParsed.
namespace {
struct SyncMeshResult {
    std::mutex mutex;
    std::condition_variable ready;
    render::DocMesh doc;
    std::string err;
    bool done = false, ok = false;
};
struct MeshLoadJob {
    uint64_t gen = 0;
    std::string path;
    bool renderOnly = false;
    bool cacheAsModel = true;
    bool cacheOnly = false;
    bool preserveSelection = false;
    bool deferWhileEditing = false;
    std::string docId;
    int revision = -1;
    std::chrono::steady_clock::time_point queuedAt;
    bool ok = false;
    std::string err;
    render::DocMesh doc;
    std::shared_ptr<SyncMeshResult> sync;
};
std::mutex              g_meshMtx;
std::condition_variable g_meshCv;
std::deque<MeshLoadJob> g_meshQueue;   // waiting for disk parse
std::vector<MeshLoadJob> g_meshReady;  // parsed, awaiting GL commit
bool                    g_meshQuit = false;
bool                    g_meshStarted = false;
std::thread             g_meshThread;

void meshLoaderMain() {
    for (;;) {
        MeshLoadJob job;
        {
            std::unique_lock<std::mutex> lk(g_meshMtx);
            g_meshCv.wait(lk, [] { return g_meshQuit || !g_meshQueue.empty(); });
            if (g_meshQuit && g_meshQueue.empty()) return;
            // Priority: a VISIBLE load (the doc the user just switched to) never waits behind
            // queued background prewarms — with an AI swarm pushing revisions to many mirror
            // tabs, the cacheOnly parse backlog used to hold the active viewport empty for the
            // whole backlog's parse time. Pick the first non-cacheOnly job, else the front.
            auto it = std::find_if(g_meshQueue.begin(), g_meshQueue.end(),
                                   [](const MeshLoadJob& j) { return !j.cacheOnly; });
            if (it == g_meshQueue.end()) it = g_meshQueue.begin();
            job = std::move(*it);
            g_meshQueue.erase(it);
        }
        job.ok = render::loadAicmFile(job.path, job.doc, job.err);
        if (job.sync) {
            auto result = std::move(job.sync);
            {
                std::lock_guard<std::mutex> lk(result->mutex);
                result->doc = std::move(job.doc);
                result->err = std::move(job.err);
                result->ok = job.ok;
                result->done = true;
            }
            result->ready.notify_one();
            ui::anim::wakeLoop();
            continue;
        }
        {
            std::lock_guard<std::mutex> lk(g_meshMtx);
            g_meshReady.push_back(std::move(job));
        }
        ui::anim::wakeLoop();
    }
}

void ensureMeshLoader() {
    if (g_meshStarted) return;
    g_meshStarted = true;
    g_meshQuit = false;
    g_meshThread = std::thread(meshLoaderMain);
}

void stopMeshLoaderThread() {
    if (!g_meshStarted) return;
    {
        std::lock_guard<std::mutex> lk(g_meshMtx);
        g_meshQuit = true;
        g_meshQueue.clear();
    }
    g_meshCv.notify_all();
    if (g_meshThread.joinable()) g_meshThread.join();
    g_meshStarted = false;
    {
        std::lock_guard<std::mutex> lk(g_meshMtx);
        g_meshReady.clear();
    }
}

bool loadParsedMeshSync(const std::string& path, render::DocMesh& doc, std::string& err) {
#ifdef __EMSCRIPTEN__
    return render::loadAicmFile(path, doc, err);
#else
    ensureMeshLoader();
    auto result = std::make_shared<SyncMeshResult>();
    {
        std::lock_guard<std::mutex> lk(g_meshMtx);
        MeshLoadJob job;
        job.path = path;
        job.sync = result;
        g_meshQueue.push_front(std::move(job));
    }
    g_meshCv.notify_one();
    std::unique_lock<std::mutex> lock(result->mutex);
    result->ready.wait(lock, [&] { return result->done; });
    doc = std::move(result->doc);
    err = std::move(result->err);
    return result->ok;
#endif
}
}  // namespace

// Join the loader before process exit. A joinable static std::thread
// dtor calls std::terminate ("terminate called without an active exception") —
// seen as a full-app crash when teardown races vendor-browser / CEF exit paths.
void AppState::stopMeshLoader() {
    stopMeshLoaderThread();
    flushConfigWrites();
}

// Commit a parsed DocMesh onto the GL-side state (setMeshes / id-pass / pick caches).
// Shared by the sync loadDocMesh path and drainMeshLoads. Requires GL context.
static bool commitDocMeshFromParsed(AppState& st, render::DocMesh& doc, std::string& err,
                                    bool renderOnly, bool shadedAlreadyBound = false);

bool AppState::loadDocMesh(const std::string& aicmPath, std::string& err, bool renderOnly,
                          bool cacheAsModel) {
    const auto retryModel = [&] {
        if (!renderOnly && cacheAsModel && tabIndexForActiveDoc() >= 0)
            core::requireDocMesh(docMirror.docId(), docMirror.revision());
    };
    if (!renderer || !idPass || !overlays) {
        err = "render objects not initialized";
        retryModel();
        return false;
    }
    render::DocMesh doc;
    if (!loadParsedMeshSync(aicmPath, doc, err)) { retryModel(); return false; }
    if (!cacheAsModel) renderer->parkActiveMeshes();
    if (!commitDocMeshFromParsed(*this, doc, err, renderOnly)) { retryModel(); return false; }
    if (!renderOnly && cacheAsModel && docMirror.hasDoc()) {
        renderer->setActiveMeshIdentity(docMirror.docId(), docMirror.revision());
        committedMeshDocId = docMirror.docId();
        committedMeshRevision = docMirror.revision();
        core::meshCatchupFinished(committedMeshDocId, committedMeshRevision, true);
        if (DocSession* s = activeSession()) {
            s->cachedAicmPath = aicmPath;
            s->cachedRevision = docMirror.revision();
            s->cachedMirror = docMirror.snapshot();
            s->cachedDocMesh = std::make_shared<render::DocMesh>(std::move(doc));
            docMeshAnalysisOwner = s->cachedDocMesh;
        }
    }
    return true;
}

void AppState::loadDocMeshAsync(const std::string& aicmPath, bool renderOnly,
                              bool cacheAsModel, bool preserveSelection,
                              bool deferWhileEditing) {
    if (aicmPath.empty()) return;
    if (!renderOnly && cacheAsModel && tabIndexForActiveDoc() >= 0)
        core::meshCatchupStarted(docMirror.docId(), docMirror.revision());
    if (!renderer || !idPass || !overlays) {
        if (!renderOnly && cacheAsModel)
            core::meshCatchupFinished(docMirror.docId(), docMirror.revision(), false);
        return;
    }
    ensureMeshLoader();
    const uint64_t gen = ++meshGen_;
    {
        std::lock_guard<std::mutex> lk(g_meshMtx);
        // Latest visible load wins, but pushed background-cache jobs are independent.
        g_meshQueue.erase(
            std::remove_if(g_meshQueue.begin(), g_meshQueue.end(),
                           [&](const MeshLoadJob& j) {
                               if (j.cacheOnly) return false;
                               if (j.docId != docMirror.docId() || j.revision != docMirror.revision())
                                   core::meshCatchupFinished(j.docId, j.revision, false);
                               return true;
                           }),
            g_meshQueue.end());
        MeshLoadJob job;
        job.gen = gen;
        job.path = aicmPath;
        job.renderOnly = renderOnly;
        job.cacheAsModel = cacheAsModel;
        job.preserveSelection = preserveSelection;
        job.deferWhileEditing = deferWhileEditing;
        if (!renderOnly && cacheAsModel) {
            job.docId = docMirror.docId();
            job.revision = docMirror.revision();
        }
        job.queuedAt = std::chrono::steady_clock::now();
        g_meshQueue.push_back(std::move(job));
    }
    g_meshCv.notify_one();
}

void AppState::cacheBackgroundDocMeshAsync(const std::string& docId, int revision,
                                           const std::string& aicmPath) {
    if (docId.empty() || revision < 0 || aicmPath.empty()) return;
    core::meshCatchupStarted(docId, revision);
    if (!renderer) { core::meshCatchupFinished(docId, revision, false); return; }
    ensureMeshLoader();
    {
        std::lock_guard<std::mutex> lk(g_meshMtx);
        g_meshQueue.erase(
            std::remove_if(g_meshQueue.begin(), g_meshQueue.end(),
                           [&](const MeshLoadJob& j) {
                               if (!j.cacheOnly || j.docId != docId) return false;
                               if (j.revision != revision)
                                   core::meshCatchupFinished(j.docId, j.revision, false);
                               return true;
                           }),
            g_meshQueue.end());
        MeshLoadJob job;
        job.path = aicmPath;
        job.cacheOnly = true;
        job.docId = docId;
        job.revision = revision;
        job.queuedAt = std::chrono::steady_clock::now();
        g_meshQueue.push_back(std::move(job));
    }
    g_meshCv.notify_one();
}

void AppState::drainMeshLoads() {
    if (!renderer || !idPass || !overlays) return;
    std::vector<MeshLoadJob> ready;
    {
        std::lock_guard<std::mutex> lk(g_meshMtx);
        if (g_meshReady.empty()) return;
        ready.swap(g_meshReady);
    }
    const uint64_t cur = meshGen_.load();
    // Prewarm at most one upload beyond the elapsed budget; a single body upload is
    // indivisible, but a swarm's backlog must not stack arbitrary commits in one frame.
    const auto prewarmDeadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(4);
    std::vector<MeshLoadJob> deferred;
    for (auto& j : ready) {
        if (!j.cacheOnly) continue;
        if (!j.ok) {
            addLog("[daemon] background mesh parse failed for " + j.docId + ": " + j.err);
            core::meshCatchupFinished(j.docId, j.revision, false);
            continue;
        }
        DocTab* cachedTab = nullptr;
        for (auto& tab : docTabs) {
            if (tab.docId == j.docId && tab.session &&
                tab.session->cachedRevision == j.revision &&
                tab.session->cachedAicmPath == j.path) {
                cachedTab = &tab;
                break;
            }
        }
        if (!cachedTab) {
            core::meshCatchupFinished(j.docId, j.revision, false);
            continue;
        }
        if (std::chrono::steady_clock::now() >= prewarmDeadline) {
            deferred.push_back(std::move(j));
            continue;
        }
        DocSession* session = cachedTab->session.get();
        auto parts = rendererPartsFromDoc(*this, j.doc);
        renderer->cacheMeshes(j.docId, j.revision, std::move(parts));
        session->cachedDocMesh = std::make_shared<render::DocMesh>(std::move(j.doc));
        cachedTab->meshCacheInvalidated = false;
        if (activeTab < 0 || activeTab >= (int)docTabs.size() ||
            docTabs[activeTab].docId != j.docId)
            core::meshCatchupFinished(j.docId, j.revision, true);
        const double ms = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - j.queuedAt).count();
        const std::string perf = "[perf] live_mesh_prewarm doc=" + j.docId + " rev=" +
            std::to_string(j.revision) + " total=" + std::to_string(ms) + "ms";
        addLog(perf);
    }
    if (!deferred.empty()) {
        std::lock_guard<std::mutex> lk(g_meshMtx);
        g_meshReady.insert(g_meshReady.begin(),
                           std::make_move_iterator(deferred.begin()),
                           std::make_move_iterator(deferred.end()));
        ui::anim::requestFrames();
    }
    // Apply only the newest visible job that still matches meshGen_.
    MeshLoadJob* best = nullptr;
    for (auto& j : ready) {
        if (!j.cacheOnly && j.gen == cur) best = &j;
    }
    for (const auto& j : ready)
        if (!j.cacheOnly && j.gen != cur &&
            (!best || j.docId != best->docId || j.revision != best->revision))
            core::meshCatchupFinished(j.docId, j.revision, false);
    if (!best) return;
    // Switching to an empty/drawing tab need not enqueue another mesh load. Do
    // not commit the old document's completion under the new selection, or pair
    // an older AICM with a newer mirror revision.
    if (!best->docId.empty() &&
        (docMirror.docId() != best->docId || docMirror.revision() != best->revision ||
         activeTab < 0 || activeTab >= (int)docTabs.size() ||
         docTabs[activeTab].docId != best->docId)) {
        core::meshCatchupFinished(best->docId, best->revision, false);
        return;
    }
    if (best->deferWhileEditing &&
        ((sketchMode && sketchMode->active()) ||
         (sketch3dMode && sketch3dMode->active()) ||
         (propertyManager && propertyManager->active()))) {
        core::meshCatchupFinished(best->docId, best->revision, false);
        return;
    }
    if (!best->ok) {
        addLog("[err] loadDocMeshAsync: " + best->err);
        core::meshCatchupFinished(best->docId, best->revision, false);
        return;
    }
    std::string err;
    if (!best->cacheAsModel) renderer->parkActiveMeshes();
    std::vector<core::SelItem> savedSelection;
    render::PickHit savedHover;
    std::string savedPickStatus;
    if (best->preserveSelection) {
        savedSelection = std::move(selection.mutableItems());
        savedHover = hover;
        savedPickStatus = std::move(pickStatus);
    }
    const bool committed = commitDocMeshFromParsed(*this, best->doc, err, best->renderOnly);
    if (best->preserveSelection) {
        selection.mutableItems() = std::move(savedSelection);
        hover = savedHover;
        pickStatus = std::move(savedPickStatus);
    }
    if (!committed) {
        if (!err.empty()) addLog("[err] loadDocMeshAsync commit: " + err);
        core::meshCatchupFinished(best->docId, best->revision, false);
        return;
    }
    if (!best->renderOnly && best->cacheAsModel && docMirror.hasDoc()) {
        renderer->setActiveMeshIdentity(docMirror.docId(), docMirror.revision());
        committedMeshDocId = best->docId;
        committedMeshRevision = best->revision;
        core::meshCatchupFinished(best->docId, best->revision, true);
        if (DocSession* s = activeSession()) {
            s->cachedAicmPath = best->path;
            s->cachedRevision = docMirror.revision();
            s->cachedMirror = docMirror.snapshot();
            s->cachedDocMesh = std::make_shared<render::DocMesh>(std::move(best->doc));
            docMeshAnalysisOwner = s->cachedDocMesh;
        }
        if (activeTab >= 0 && activeTab < (int)docTabs.size() &&
            docTabs[activeTab].docId == docMirror.docId()) {
            docTabs[activeTab].meshCacheInvalidated = false;
        }
        const double ms = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - best->queuedAt).count();
        const std::string perf = "[perf] live_mesh_refresh doc=" + docMirror.docId() + " rev=" +
            std::to_string(docMirror.revision()) + " total=" + std::to_string(ms) + "ms";
        addLog(perf);
    }
}

static bool commitDocMeshFromParsed(AppState& st, render::DocMesh& doc, std::string& err,
                                    bool renderOnly, bool shadedAlreadyBound) {
    if (!st.renderer || !st.idPass || !st.overlays) {
        err = "render objects not initialized";
        return false;
    }
    if (renderOnly) {
        // Render-only (blend live preview): replace ONLY the shaded meshes; leave the id-pass,
        // st.overlays, pick caches, st.selection and st.hasDocMesh as the base-body load left them, so picks
        // and highlights keep resolving against the un-rounded base while the user sees the round.
        auto rpms = rendererPartsFromDoc(st, doc, false);
        st.renderer->setMeshes(std::move(rpms));
        return true;
    }
    st.parkMeshAnalysis();
    if (doc.bodies.empty()) {
        // C48: a rebuild can legitimately produce ZERO bodies — every solid feature rolled
        // back (rollback bar), suppressed, or deleted, or a sketch-only document. That is an
        // empty MODEL, not a load error. Clear the shaded view + pick caches so the viewport
        // tracks the document; before, the stale previous mesh kept rendering and the
        // rollback bar looked like it did nothing.
        if (st.renderer && !shadedAlreadyBound)
            st.renderer->setMeshes(std::vector<Renderer::PartMesh>{});
        clearPickBuffers(st.idPass, st.overlays);
        st.bodyVisible.clear();
        st.docBodyIds.clear();
        st.applyBodyVisibility();
        st.faceNormalCache.clear();
        st.edgeCentroidCache.clear();
        st.bodyBBoxCache.clear();
        st.faceBBoxCache.clear();
        st.selection.clear();
        st.hover = render::PickHit{};
        st.pickStatus.clear();
        st.docMeshHasScalar = false;
        st.docMeshHasDisp = false;
        st.hasDocMesh = false;   // the empty-doc fallback (datum planes + camera) takes over
        // Keep plane auto-hide in sync for sketch-only / rolled-back docs (no solid mesh).
        if (st.docMirror.hasDoc())
            st.applyDocDrivenVisibility(st.docMirror.tree());
        return true;
    }

    // Shaded view: render each body with its AICM color (camera bounds come from here).
    // C19: attach any SCLR / DISP overlay arrays so the st.renderer can colormap + deform.
    std::vector<uint32_t> newBodyIds;  // AICM body id per st.renderer slot (parallels pms)
    st.docMeshHasScalar = false;
    st.docMeshHasDisp = false;
    for (const auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        newBodyIds.push_back(b.body_id);
        // Prefer the selected result field; fall back to the first valid field for this body.
        const render::ScalarField* chosen = nullptr;
        for (const auto& sc : doc.scalars) {
            if (sc.body_id != b.body_id || sc.valueCount != b.n_verts) continue;
            if (sc.name == st.activeScalarField) { chosen = &sc; break; }
            if (!chosen) chosen = &sc;
        }
        if (chosen) {
            st.docMeshHasScalar |= chosen->valueCount != 0;
        }
        // First displacement field for this body (FEA deformed shape).
        for (const auto& df : doc.displacements) {
            if (df.body_id == b.body_id && df.vectorSize == (size_t)b.n_verts * 3) {
                if (df.vectorSize != 0) {
                    st.docMeshHasDisp = true;
                    st.dispScaleHint = df.scale_hint;
                }
                break;
            }
        }
    }
    // C48: unconditional — pms can be empty when every body has no triangles; keeping the
    // previous meshes would desync the viewport from the document (same stale-mesh bug as
    // the zero-bodies case above).
    if (!shadedAlreadyBound) {
        auto pms = rendererPartsFromDoc(st, doc);
        st.renderer->setMeshes(std::move(pms));
    }


    if (!st.idPass->setDoc(doc, err)) return false;
    if (!st.overlays->setDoc(doc, err)) return false;

    // Solid Bodies visibility: a rebuild with the SAME body-id list keeps the user's hidden set
    // (hide-by-ordinal is stable while the body set is unchanged); a different list — a feature
    // added/removed a body, so ordinals realigned — resets everything to visible. (Documented
    // limitation: hides are by ordinal, not by persistent body identity.)
    if (st.docBodyIds != newBodyIds || st.bodyVisible.size() != newBodyIds.size())
        st.bodyVisible.assign(newBodyIds.size(), 1);
    st.docBodyIds = std::move(newBodyIds);
    st.applyBodyVisibility();

    st.faceNormalCache = std::move(doc.faceNormalCache);
    st.edgeCentroidCache = std::move(doc.edgeCentroidCache);
    st.bodyBBoxCache = std::move(doc.bodyBBoxCache);
    st.faceBBoxCache = std::move(doc.faceBBoxCache);

    st.selection.clear();
    st.hover = render::PickHit{};
    st.pickStatus.clear();
    st.hasDocMesh = true;
    // Doc-driven visibility runs after body/datum geometry is bound: persisted assembly component
    // masks plus consumed-plane auto-hide from the active feature dependency graph.
    st.applyDocDrivenVisibility(st.docMirror.tree());
    return true;
}

bool AppState::bindCachedDocMesh(DocSession& s, std::string& err) {
    if (!renderer || !idPass || !overlays) {
        err = "render objects not initialized";
        return false;
    }
    if (!s.cachedMirror.valid() || !s.cachedDocMesh ||
        s.cachedMirror.revision != s.cachedRevision) {
        err = "no immutable document cache";
        return false;
    }
    // A queued temporary overlay must not overwrite this restored model on a later drain.
    ++meshGen_;
    if (!docMirror.restoreSnapshot(s.cachedMirror)) {
        err = "invalid document mirror cache";
        return false;
    }
    const bool gpuBound = renderer->bindCachedMeshes(s.cachedMirror.docId, s.cachedRevision);
    if (!gpuBound && !s.cachedDocMesh->bodies.empty()) {
        if (s.cachedAicmPath.empty()) {
            err = "cached tessellation has no reload path";
            return false;
        }
        unloadDocMesh();
        loadDocMeshAsync(s.cachedAicmPath, false, true, true);
        return true;
    }
    if (!commitDocMeshFromParsed(*this, *s.cachedDocMesh, err, false, gpuBound)) {
        core::requireDocMesh(s.cachedMirror.docId, s.cachedRevision);
        return false;
    }
    docMeshAnalysisOwner = s.cachedDocMesh;
    renderer->setActiveMeshIdentity(s.cachedMirror.docId, s.cachedRevision);
    committedMeshDocId = s.cachedMirror.docId;
    committedMeshRevision = s.cachedRevision;
    core::meshCatchupFinished(committedMeshDocId, committedMeshRevision, true);
    return true;
}

void AppState::parkMeshAnalysis() {
    if (auto owner = docMeshAnalysisOwner.lock()) {
        owner->faceNormalCache = std::move(faceNormalCache);
        owner->edgeCentroidCache = std::move(edgeCentroidCache);
        owner->bodyBBoxCache = std::move(bodyBBoxCache);
        owner->faceBBoxCache = std::move(faceBBoxCache);
    }
    docMeshAnalysisOwner.reset();
}

void AppState::unloadDocMesh() {
    // An older in-flight load must not resurrect geometry after an empty push
    // or after the user switches to a tab with no model.
    ++meshGen_;
    committedMeshDocId.clear();
    committedMeshRevision = -1;
    parkMeshAnalysis();
    if (renderer) renderer->setMeshes(std::vector<Renderer::PartMesh>{});
    clearPickBuffers(idPass, overlays);
    faceNormalCache.clear();
    edgeCentroidCache.clear();
    bodyBBoxCache.clear();
    faceBBoxCache.clear();
    docMeshHasScalar = false;
    docMeshHasDisp = false;
    hasDocMesh = false;
    applyBodyVisibility();
}

void AppState::clearViewportForNewDoc() {
    ++meshGen_;
    committedMeshDocId.clear();
    committedMeshRevision = -1;
    parkMeshAnalysis();
    if (renderer) {
        renderer->parkActiveMeshes();
        renderer->setMeshes(std::vector<Renderer::PartMesh>{});
    }
    clearPickBuffers(idPass, overlays);
    hasDocMesh = false;
    // Solid Bodies visibility: a fresh doc starts with no bodies and nothing hidden (stale hides
    // must never leak into the next document).
    bodyVisible.clear();
    docBodyIds.clear();
    componentHiddenOverrides.clear();
    hiddenFeatures.clear();
    autoHiddenPlanes.clear();
    datumPlanesUserToggled.clear();
    refGeomUserToggled.clear();
    // W2-ref: axes start clean on tab/new-doc; refilled by inject path next rebuild.
    // Seed axisDatumIds so force-show / FM listing works pre-inject. Axes default VISIBLE
    // on an empty doc (NOT in hiddenFeatures) — matching planes/origin auto-show. Auto-hide
    // once hasSolidContent is applied by applyDocDrivenVisibility + viewport_panel (and only
    // sticks after axesUserToggled). Explicitly erase residual axis ids after clear.
    axisDatumIds.clear();
    axisAutoCache.clear();
    axisInjectPlaneFp.clear();
    axisDisplayNames.clear();
    // Origin point: VISIBLE by default on an empty doc (same rule as planes/axes). It is NOT
    // seeded into hiddenFeatures; applyDocDrivenVisibility inserts/erases it with hasSolidContent
    // (unless originUserToggled), so the FM eye — which reads hiddenFeatures — is correct from
    // frame 0. (Replaces the old hidden-by-default seed.)
    hiddenFeatures.erase("point.origin");
    // Default reference axes: visible on empty/sketch-only docs (FM eye OPEN).
    hiddenFeatures.erase("axis.x");
    hiddenFeatures.erase("axis.y");
    hiddenFeatures.erase("axis.z");
    axisDatumIds.insert("axis.x");
    axisDatumIds.insert("axis.y");
    axisDatumIds.insert("axis.z");
    // Fresh empty doc: planes + origin are VISIBLE by default. Seed the FM-eye flags so the
    // FeatureManager (drawn before the viewport publishes these each frame) shows OPEN eyes from
    // frame 0 instead of a stale "hidden" glyph on planes/origin that are actually drawn.
    planeVisEff[0] = planeVisEff[1] = planeVisEff[2] = true;
    originVisEff = true;
    applyBodyVisibility();
    applyDatumVisibility();
    asmParts.clear();
    selectedPart = -1;
    selection.clear();
    modelInfo.clear();
    faceNormalCache.clear();
    edgeCentroidCache.clear();
    bodyBBoxCache.clear();
    faceBBoxCache.clear();
    // W1-B (item 5): drop the tree's selection + in-progress rename/pending-op transients so a fresh
    // doc starts clean (the selection state now lives here; the rename buffer in feature_tree.cpp is
    // cleared via the exposed reset hook).
    selectedFeatureId.clear();
    treeScrollToSelection = false;
    ui::featureTreeResetTransient();
    // Wave-2 shared UI state: a fresh doc cancels any plane-pick step, tree-driven face highlight,
    // and pending zoom-to-bbox request.
    sketchPlanePickActive = false;
    featureHighlightFaces.clear();
    zoomToBBoxRequested = false;
    // A fresh part re-enters the auto-show "pick a plane to start" state until the user toggles.
    planesUserToggled = false;
    originUserToggled = false;
    axesUserToggled = false;
    axesUserToggledIds.clear();
    showPlane[0] = showPlane[1] = showPlane[2] = false;
}

// Multi-document: park the LEAVING tab's live per-doc value state into its DocSession (move out, so
// AppState's fields are left empty — the entering doc's state is then restored over them). The owned
// PM / SketchMode / EvaluateDialogs are not touched here; they live on the session and are repointed
// by reseatActiveSubsystems. KEEP THE FIELD LIST IN SYNC WITH restoreDocState + clearViewportForNewDoc.
void AppState::captureDocState(DocSession& s) {
    // Selection items: swap the vector out of the live Selection WITHOUT disturbing its observer
    // (the observer is wired once on AppState::selection and must survive every switch).
    s.selItems.clear();
    std::swap(s.selItems, selection.mutableItems());
    s.hover        = hover;
    s.pickStatus   = std::move(pickStatus);
    s.bodyVisible  = std::move(bodyVisible);
    s.docBodyIds   = std::move(docBodyIds);
    s.componentHiddenOverrides = std::move(componentHiddenOverrides);
    parkMeshAnalysis();
    s.hiddenFeatures = std::move(hiddenFeatures);
    s.autoHiddenPlanes = std::move(autoHiddenPlanes);
    s.datumPlanesUserToggled = std::move(datumPlanesUserToggled);
    s.refGeomUserToggled = std::move(refGeomUserToggled);
    for (int i = 0; i < 3; ++i) s.showPlane[i] = showPlane[i];
    s.planesUserToggled = planesUserToggled;
    s.originUserToggled = originUserToggled;
    s.axesUserToggled = axesUserToggled;
    s.axesUserToggledIds = std::move(axesUserToggledIds);
    s.modelInfo    = std::move(modelInfo);
    s.asmParts     = std::move(asmParts);
    s.asmFit       = std::move(asmFit);
    s.asmStl       = std::move(asmStl);
    s.selectedPart = selectedPart;
    s.selectedFeatureId     = std::move(selectedFeatureId);
    s.treeScrollToSelection = treeScrollToSelection;
    s.featureHighlightFaces = std::move(featureHighlightFaces);
    s.sketchPlanePickActive = sketchPlanePickActive;
}

// Multi-document: restore the ENTERING tab's parked MODEL-side state (hidden-body mask BEFORE the
// mesh load so loadDocMesh preserves it, plus the readouts / tree highlight). The viewport SELECTION
// (selItems / hover / pickStatus) is intentionally left on `s` and restored by switchToTab AFTER
// loadDocMesh — loadDocMesh clears the live selection, so restoring it earlier would be wiped.
void AppState::restoreDocState(DocSession& s) {
    bodyVisible  = std::move(s.bodyVisible);
    docBodyIds   = std::move(s.docBodyIds);
    componentHiddenOverrides = std::move(s.componentHiddenOverrides);
    hiddenFeatures = std::move(s.hiddenFeatures);
    autoHiddenPlanes = std::move(s.autoHiddenPlanes);
    datumPlanesUserToggled = std::move(s.datumPlanesUserToggled);
    refGeomUserToggled = std::move(s.refGeomUserToggled);
    for (int i = 0; i < 3; ++i) showPlane[i] = s.showPlane[i];
    planesUserToggled = s.planesUserToggled;
    originUserToggled = s.originUserToggled;
    axesUserToggled = s.axesUserToggled;
    axesUserToggledIds = std::move(s.axesUserToggledIds);
    applyDatumVisibility();   // reflect this tab's datum hides in the renderer
    modelInfo    = std::move(s.modelInfo);
    asmParts     = std::move(s.asmParts);
    asmFit       = std::move(s.asmFit);
    asmStl       = std::move(s.asmStl);
    selectedPart = s.selectedPart;
    selectedFeatureId     = std::move(s.selectedFeatureId);
    treeScrollToSelection = s.treeScrollToSelection;
    featureHighlightFaces = std::move(s.featureHighlightFaces);
    sketchPlanePickActive = s.sketchPlanePickActive;
}

// ---- in-context component edit (SolidWorks "Edit Component") ----
// Make a component's part-doc the REAL ACTIVE doc while drawing the assembly as a dimmed ghost. The
// active-doc switch REUSES the exact path openComponentPart (feature_tree.cpp) uses: dedup the source
// path against already-open non-handoff tabs and switchToTab() it (which binds the parked immutable
// mirror/tessellation/GPU cache), else set requestOpenPath so app.cpp opens it next frame.
void AppState::enterComponentEdit(const std::string& componentId, const std::string& sourceAbsPath) {
    if (componentEdit.active || sourceAbsPath.empty()) return;
    namespace fs = std::filesystem;
    std::error_code ec;
    fs::path want = fs::weakly_canonical(fs::path(sourceAbsPath), ec);
    if (ec) want = fs::path(sourceAbsPath).lexically_normal();

    // Remember the assembly doc as the return target BEFORE we switch away from it. Prefer the active
    // tab's docId (the assembly the component belongs to); fall back to the mirror's current doc.
    std::string returnDocId;
    if (activeTab >= 0 && activeTab < (int)docTabs.size())
        returnDocId = docTabs[activeTab].docId;
    if (returnDocId.empty()) returnDocId = docMirror.docId();

    // Capture the assembly's CURRENT tessellated mesh into the renderer ghost NOW, while subs_ still
    // holds it (before any switch frees/replaces subs_). Pure GPU-handle move — no re-tessellate.
    if (renderer) {
        renderer->captureCurrentAsGhost();

        // Relocate the ghost so the edited component's ghost copy lands on the live part at its own
        // modelling origin (SolidWorks in-context edit). The ghost mesh was baked with each component
        // already placed at its SOLVED assembly transform (the mate-solved placement is persisted back
        // into doc["components"][].transform in session.cpp::rebuild_assembly, so the mirror tree
        // carries exactly what the ghost was baked at). We therefore pre-multiply the whole ghost by
        // inverse(thisComponent.placement): the edited component's ghost copy collapses onto the live
        // part at origin while the rest of the assembly wraps correctly around it. Identity placement
        // (the fixed/first component) inverts to identity → no shift.
        //
        // Convention: the stored 16-float transform is ROW-MAJOR (fed to gp_Trsf::SetValues in
        // core/doc/assembly.cpp::trsf_from_xform16 — m[0..3]=row0, m[4..7]=row1, m[8..11]=row2, so the
        // translation sits at m[3],m[7],m[11]). The renderer's Mat4 is COLUMN-MAJOR (translation at
        // m[12..14]). For a rigid transform, inverse = [Rᵀ | -Rᵀt]; the row→column transpose and the
        // rotation-inverse transpose cancel, so the rotation block copies straight across and only the
        // translation is recomputed. setGhostTransform() consumes column-major, so we emit that here.
        std::vector<double> a = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};  // kIdent16
        const nlohmann::json& tree = docMirror.tree();
        if (tree.contains("components") && tree["components"].is_array()) {
            for (const auto& c : tree["components"]) {
                if (c.value("id", std::string()) != componentId) continue;
                if (c.contains("transform") && c["transform"].is_array() &&
                    c["transform"].size() == 16) {
                    auto v = c["transform"].get<std::vector<double>>();
                    if (v.size() == 16) a = std::move(v);
                }
                break;
            }
        }
        // Column-major inverse of the rigid row-major placement `a`.
        float inv[16];
        // Rotation block: Rᵀ (transpose-for-inverse) then transpose again for row→column ⇒ copy across.
        inv[0]  = (float)a[0];  inv[4]  = (float)a[4];  inv[8]  = (float)a[8];   inv[12] = 0.0f;
        inv[1]  = (float)a[1];  inv[5]  = (float)a[5];  inv[9]  = (float)a[9];   inv[13] = 0.0f;
        inv[2]  = (float)a[2];  inv[6]  = (float)a[6];  inv[10] = (float)a[10];  inv[14] = 0.0f;
        inv[3]  = 0.0f;         inv[7]  = 0.0f;         inv[11] = 0.0f;          inv[15] = 1.0f;
        // Translation of the inverse = -Rᵀt, landing in the column-major translation slots (12,13,14).
        const double tx = a[3], ty = a[7], tz = a[11];  // row-major translation of `a`
        inv[12] = -(float)(a[0] * tx + a[4] * ty + a[8]  * tz);
        inv[13] = -(float)(a[1] * tx + a[5] * ty + a[9]  * tz);
        inv[14] = -(float)(a[2] * tx + a[6] * ty + a[10] * tz);
        renderer->setGhostTransform(inv);
    }

    componentEdit.active         = true;
    componentEdit.componentId    = componentId;
    componentEdit.sourcePath     = sourceAbsPath;
    componentEdit.assemblyDocId  = returnDocId;

    // Dedup against already-open non-handoff tabs: switch to the first match (cached switch path)
    // rather than reopening. Mirrors openComponentPart's dedup exactly.
    for (int i = 0; i < (int)docTabs.size(); ++i) {
        const DocTab& t = docTabs[i];
        if (t.aiHandoff || t.path.empty()) continue;
        std::error_code e2;
        fs::path have = fs::weakly_canonical(fs::path(t.path), e2);
        if (e2) have = fs::path(t.path).lexically_normal();
        if (have == want) { switchToTab(i); return; }
    }
    // Not open yet: queue the GL-thread open pump (drained next frame by app.cpp). The ghost shows
    // alone for that one frame until the component mesh loads.
    requestOpenPath = sourceAbsPath;
}

// Exit component-edit keeps its explicit component save, but assembly updates
// already arrive as in-memory refresh snapshots and need no forced rebuild.
void AppState::exitComponentEdit() {
    if (!componentEdit.active) return;
    const std::string returnDocId = componentEdit.assemblyDocId;
    const std::string sourcePath = componentEdit.sourcePath;
    const std::string selectedDocId =
        activeTab >= 0 && activeTab < (int)docTabs.size() ? docTabs[activeTab].docId : std::string();
    std::string componentDocId;
    std::error_code ec;
    auto source = std::filesystem::weakly_canonical(sourcePath, ec);
    if (ec) source = std::filesystem::path(sourcePath).lexically_normal();
    for (const auto& tab : docTabs) {
        if (tab.path.empty()) continue;
        ec.clear();
        auto path = std::filesystem::weakly_canonical(tab.path, ec);
        if (ec) path = std::filesystem::path(tab.path).lexically_normal();
        if (path == source) { componentDocId = tab.docId; break; }
    }
    componentEdit = ComponentEditCtx{};
    if (renderer) renderer->clearGhost();

    auto switchBack = [this, returnDocId, selectedDocId]() {
        // A different selection while the save was in flight is newer user intent.
        if (componentEdit.active || activeTab < 0 || activeTab >= (int)docTabs.size() ||
            docTabs[activeTab].docId != selectedDocId) return;
        for (int i = 0; i < (int)docTabs.size(); ++i) {
            if (docTabs[i].docId == returnDocId) { switchToTab(i); break; }
        }
    };
    if (!sourcePath.empty() && !componentDocId.empty() && workerStarted) {
        ++rebuildsInFlight;
        rpc.call("doc_save", nlohmann::json{{"doc_id", componentDocId}, {"path", sourcePath}},
                 [this, switchBack, componentDocId](const core::RpcResult& r) {
                     --rebuildsInFlight;
                     if (!r.ok) {
                         addLog("[asm] exit-edit: component save failed: " + r.err);
                         toast(ui::ToastLevel::Warn, "Component didn't save");
                     } else {
                         addLog("[asm] exit-edit: component saved (" + componentDocId + ")");
                     }
                     switchBack();
                 }, 120000);
        return;
    }
    switchBack();
}

bool AppState::componentHidden(const std::string& id, bool documentHidden) const {
    const auto it = componentHiddenOverrides.find(id);
    return it == componentHiddenOverrides.end() ? documentHidden : it->second;
}

void AppState::setComponentHidden(const std::string& id, bool hidden) {
    componentHiddenOverrides[id] = hidden;
    applyDocDrivenVisibility(docMirror.tree());
    if (hidden) {
        selection.clear();
        hover = {};
    }
}

// Solid Bodies hide/show: push the host-side visibility mask to the renderer (per-slot draw
// skip) and to the id pass (hidden-body pick skip), keeping both perfectly in sync. Safe to call
// with no doc loaded (both sinks tolerate empty state).
void AppState::applyBodyVisibility() {
    if (renderer) {
        size_t n = std::min(bodyVisible.size(), renderer->bodyCount());
        for (size_t i = 0; i < n; ++i) renderer->setBodyVisible(i, bodyVisible[i] != 0);
    }
    if (idPass) {
        std::set<uint32_t> hidden;
        size_t n = std::min(bodyVisible.size(), docBodyIds.size());
        for (size_t i = 0; i < n; ++i)
            if (!bodyVisible[i]) hidden.insert(docBodyIds[i]);
        idPass->setHiddenBodies(hidden);
    }
}

void AppState::applyDatumVisibility() {
    if (renderer) renderer->setHiddenDatums(hiddenFeatures);
}

// Default-plane → axis letter (Front/X, Right/Y, Top/Z). Empty if not a reserved plane.
static const char* reservedPlaneAxisLetter(const std::string& planeId) {
    if (planeId == "plane.front") return "X";
    if (planeId == "plane.right") return "Y";
    if (planeId == "plane.top")   return "Z";
    return nullptr;
}

static const char* reservedPlaneDisplayName(const std::string& planeId) {
    if (planeId == "plane.front") return "Front";
    if (planeId == "plane.top")   return "Top";
    if (planeId == "plane.right") return "Right";
    return nullptr;
}

std::string AppState::formatAutoAxisDisplayName(
    const std::string& planeIdA, const std::string& planeIdB,
    const std::unordered_map<std::string, std::string>& planeNameById) {
    auto nameOf = [&](const std::string& id) -> std::string {
        auto it = planeNameById.find(id);
        if (it != planeNameById.end() && !it->second.empty()) return it->second;
        if (const char* rn = reservedPlaneDisplayName(id)) return rn;
        return id;
    };
    const char* letterA = reservedPlaneAxisLetter(planeIdA);
    const char* letterB = reservedPlaneAxisLetter(planeIdB);
    // Prefer the default plane in the pair for the axis letter; the OTHER plane is the user label.
    if (letterA && !letterB) {
        return nameOf(planeIdB) + " " + letterA + " Axis";
    }
    if (letterB && !letterA) {
        return nameOf(planeIdA) + " " + letterB + " Axis";
    }
    // Both reserved (should not appear as axis.auto) or both user: fall back to a clean pair label.
    if (letterA && letterB) {
        // e.g. Front ∩ Top is axis.x already — keep a readable pair name if emitted.
        return std::string(letterA) + "/" + letterB + " Axis";
    }
    return nameOf(planeIdA) + " / " + nameOf(planeIdB) + " Axis";
}

void AppState::rebuildAxisDisplayNames(const nlohmann::json& tree) {
    std::unordered_map<std::string, std::string> planeNameById;
    planeNameById["plane.front"] = "Front";
    planeNameById["plane.top"]   = "Top";
    planeNameById["plane.right"] = "Right";
    if (tree.is_object()) {
        auto fi = tree.find("features");
        if (fi != tree.end() && fi->is_array()) {
            for (const auto& f : *fi) {
                if (!f.is_object()) continue;
                const std::string ty = f.value("type", std::string());
                const std::string id = f.value("id", std::string());
                if (id.empty()) continue;
                if (ty == "plane" || ty == "datum_plane" ||
                    ty == "axis" || ty == "datum_axis" ||
                    ty == "point" || ty == "datum_point" ||
                    ty == "csys" || ty == "coordinate_system" ||
                    ty == "ref_plane" || ty == "ref_axis" ||
                    ty == "ref_point" || ty == "ref_csys") {
                    const std::string nm = f.value("name", std::string());
                    planeNameById[id] = nm.empty() ? id : nm;
                }
            }
        }
    }

    axisDisplayNames.clear();
    axisDisplayNames["axis.x"] = "X Axis";
    axisDisplayNames["axis.y"] = "Y Axis";
    axisDisplayNames["axis.z"] = "Z Axis";

    // Auto plane-intersection axes from the inject cache (id = axis.auto:<a>|<b>).
    for (auto& ent : axisAutoCache) {
        const std::string& id = ent.id;
        if (id.rfind("axis.auto:", 0) != 0) {
            if (!ent.name.empty()) axisDisplayNames[id] = ent.name;
            continue;
        }
        const std::string pair = id.substr(std::string("axis.auto:").size());
        const size_t bar = pair.find('|');
        if (bar == std::string::npos) {
            axisDisplayNames[id] = ent.name.empty() ? id : ent.name;
            continue;
        }
        const std::string a = pair.substr(0, bar);
        const std::string b = pair.substr(bar + 1);
        ent.name = formatAutoAxisDisplayName(a, b, planeNameById);
        axisDisplayNames[id] = ent.name;
    }

    // User-authored axis features (and any other axisDatumIds not already named).
    for (const auto& id : axisDatumIds) {
        if (axisDisplayNames.count(id)) continue;
        auto it = planeNameById.find(id);
        if (it != planeNameById.end() && !it->second.empty())
            axisDisplayNames[id] = it->second;
        else
            axisDisplayNames[id] = id;
    }
}


// The three origin planes are renderer-owned overlays rather than datum features in the tree.
static bool isDefaultPlaneFid(const std::string& fid) {
    return fid == "plane.front" || fid == "plane.top" || fid == "plane.right";
}

// Sketches and datum construction features do not consume their references by themselves. A plane
// only becomes clutter once a downstream model feature reaches it through this dependency graph —
// OR once the doc has solid content (hasSolidContent), matching default-plane/axis policy.
static bool isDatumPlaneType(const std::string& type) {
    return type == "plane" || type == "datum_plane" || type == "ref_plane";
}

static bool isReferenceOnlyFeature(const std::string& type) {
    return isDatumPlaneType(type) ||
           type == "axis" || type == "datum_axis" || type == "ref_axis" ||
           type == "point" || type == "datum_point" || type == "ref_point" ||
           type == "csys" || type == "coordinate_system" || type == "ref_csys" ||
           type == "sketch" || type == "sketch3d";
}

// Collect feature-id and reserved-plane references from any canonical refs value. Selector refs use
// nested objects (feature/owner_feature/sketch), feature lists use arrays of bare ids, and the three
// default planes are bare strings, so recursively walking only `refs` is both complete and avoids
// mistaking arbitrary parameter strings for dependencies.
static void collectFeatureRefs(
    const nlohmann::json& value,
    const std::unordered_map<std::string, const nlohmann::json*>& activeFeatures,
    std::unordered_set<std::string>& featureDeps,
    std::unordered_set<std::string>& consumedPlanes) {
    if (value.is_string()) {
        const std::string ref = value.get<std::string>();
        if (isDefaultPlaneFid(ref)) consumedPlanes.insert(ref);
        if (activeFeatures.count(ref)) featureDeps.insert(ref);
        return;
    }
    if (value.is_array()) {
        for (const auto& item : value)
            collectFeatureRefs(item, activeFeatures, featureDeps, consumedPlanes);
        return;
    }
    if (value.is_object()) {
        for (auto it = value.begin(); it != value.end(); ++it)
            collectFeatureRefs(it.value(), activeFeatures, featureDeps, consumedPlanes);
    }
}

void AppState::applyDocDrivenVisibility(const nlohmann::json& tree) {
    if (!tree.is_object()) return;
    const bool isAsm = tree.value("schema", std::string()) == "aicad.asm/1";

    // Cache only graph-derived facts. Visibility masks/eye flags still synchronize on
    // every call because the same tree may be applied before and after a mesh/datum bind.
    struct GraphCache {
        const AppState* owner = nullptr;
        const nlohmann::json* source = nullptr;
        std::string docId, rollbackAfter;
        int revision = -1;
        nlohmann::json features;
        std::unordered_set<std::string> userPlanes, consumedPlanes;
        std::vector<std::string> referenceGeometry;
        bool hasSolidFeatures = false;
    };
    static GraphCache cache;  // one bounded active-document graph
    static const nlohmann::json emptyFeatures = nlohmann::json::array();
    auto fi = tree.find("features");
    const auto& features = fi != tree.end() && fi->is_array() ? *fi : emptyFeatures;
    const auto rollback = tree.find("rollback_after");
    const std::string rollbackAfter = rollback != tree.end() && rollback->is_string()
        ? rollback->get<std::string>() : std::string();
    const bool sameDoc = cache.owner == this && cache.docId == docMirror.docId();
    const bool sameRevision = sameDoc && cache.source == &tree &&
        &tree == &docMirror.tree() && cache.revision == docMirror.revision();
    if (!sameDoc || cache.rollbackAfter != rollbackAfter ||
        (!sameRevision && cache.features != features)) {
        cache.features = features;
        cache.rollbackAfter = rollbackAfter;
        cache.userPlanes.clear();
        cache.consumedPlanes.clear();
        cache.referenceGeometry.clear();
        cache.hasSolidFeatures = false;
        std::unordered_map<std::string, const nlohmann::json*> activeFeatures;
        activeFeatures.reserve(features.size());
        bool pastRollback = rollbackAfter == "start";
        for (const auto& f : features) {
            if (!f.is_object()) continue;
            const std::string type = f.value("type", std::string());
            const bool suppressed = f.value("suppressed", false);
            if (!suppressed && !type.empty() && !isReferenceOnlyFeature(type))
                cache.hasSolidFeatures = true;
            if (pastRollback) continue;
            const std::string fid = f.value("id", std::string());
            if (!fid.empty() && !suppressed) {
                activeFeatures.emplace(fid, &f);
                if (isDatumPlaneType(type)) cache.userPlanes.insert(fid);
                if (type == "point" || type == "datum_point" || type == "ref_point" ||
                    type == "csys" || type == "coordinate_system" || type == "ref_csys")
                    cache.referenceGeometry.push_back(fid);
            }
            if (!rollbackAfter.empty() && fid == rollbackAfter) pastRollback = true;
        }
        std::unordered_set<std::string> visited;
        visited.reserve(activeFeatures.size());
        std::unordered_set<std::string> pending;
        for (const auto& entry : activeFeatures) {
            const auto& root = *entry.second;
            if (isReferenceOnlyFeature(root.value("type", std::string()))) continue;
            auto refs = root.find("refs");
            if (refs != root.end())
                collectFeatureRefs(*refs, activeFeatures, pending, cache.consumedPlanes);
        }
        while (!pending.empty()) {
            auto next = pending.begin();
            const std::string depId = *next;
            pending.erase(next);
            if (!visited.insert(depId).second) continue;
            auto dep = activeFeatures.find(depId);
            if (dep == activeFeatures.end()) continue;
            const auto& feature = *dep->second;
            if (isDatumPlaneType(feature.value("type", std::string())))
                cache.consumedPlanes.insert(depId);
            auto refs = feature.find("refs");
            if (refs != feature.end())
                collectFeatureRefs(*refs, activeFeatures, pending, cache.consumedPlanes);
        }
    }
    cache.owner = this;
    cache.source = &tree;
    cache.docId = docMirror.docId();
    cache.revision = docMirror.revision();
    const auto& userPlanes = cache.userPlanes;
    const auto& consumedPlanes = cache.consumedPlanes;
    const auto components = tree.find("components");
    const bool hasSolidContent = cache.hasSolidFeatures ||
        (components != tree.end() && components->is_array() && !components->empty());
    // Seed the FM-eye visibility flags (planeVisEff/originVisEff) from the state computed HERE, so
    // the FeatureManager eye is correct on the very first frame after a doc loads/rebuilds — the FM
    // draws BEFORE the viewport publishes these each frame, so without this a fresh doc showed a
    // stale "hidden" eye on planes/origin that are actually drawn.
    auto syncEffFlags = [&]() {
        static const char* kPF[3] = {"plane.front", "plane.top", "plane.right"};
        for (int i = 0; i < 3; ++i)
            planeVisEff[i] = planesUserToggled ? showPlane[i]
                                               : (autoHiddenPlanes.count(kPF[i]) == 0);
        originVisEff = originUserToggled ? (hiddenFeatures.count("point.origin") == 0)
                                         : !hasSolidContent;
    };

    if (isAsm) {
        // The engine builds one body per NON-suppressed component,
        // 1-based ordinal == body_id (assembly.cpp).
        auto ci = tree.find("components");
        if (ci != tree.end() && ci->is_array()) {
            uint32_t ordinal = 0;
            for (const auto& c : *ci) {
                if (!c.is_object()) continue;
                if (c.value("suppressed", false)) continue;
                ++ordinal;
                uint8_t vis = componentHidden(c.value("id", std::string()),
                                              c.value("hidden", false)) ? 0 : 1;
                for (size_t slot = 0; slot < docBodyIds.size(); ++slot)
                    if (docBodyIds[slot] == ordinal && slot < bodyVisible.size())
                        bodyVisible[slot] = vis;
            }
            applyBodyVisibility();
        }
        // Do not return: assembly-level features can consume datum planes too.
    }

    // No features array: still apply default-plane hasSolidContent rule (e.g. components-only asm).
    if (fi == tree.end() || !fi->is_array()) {
        if (!planesUserToggled) {
            static const char* kDefaultPlanes[3] = {"plane.front", "plane.top", "plane.right"};
            if (hasSolidContent) {
                for (const char* p : kDefaultPlanes) autoHiddenPlanes.insert(p);
            } else {
                for (const char* p : kDefaultPlanes) autoHiddenPlanes.erase(p);
            }
        }
        // Axis eye sync (same as the full-features path below).
        // Per-id sticky: axesUserToggledIds honors hiddenFeatures as-is.
        // Defaults still gate on global axesUserToggled; axis.auto:* always
        // auto-hide on solid unless that specific id was user-toggled.
        {
            bool changed = false;
            auto syncAxis = [&](const std::string& id) {
                if (id.empty()) return;
                if (axesUserToggledIds.count(id)) return;
                const bool isAuto = id.rfind("axis.auto:", 0) == 0;
                // Defaults: only auto-manage while user has not taken aggregate control.
                if (!isAuto && axesUserToggled) return;
                if (hasSolidContent) {
                    if (hiddenFeatures.insert(id).second) changed = true;
                } else {
                    if (hiddenFeatures.erase(id) != 0) changed = true;
                }
            };
            syncAxis("axis.x");
            syncAxis("axis.y");
            syncAxis("axis.z");
            for (const auto& id : axisDatumIds) syncAxis(id);
            if (changed) applyDatumVisibility();
        }
        // Origin: same persistent sync (FM eye reads hiddenFeatures).
        if (!originUserToggled) {
            if (hasSolidContent) hiddenFeatures.insert("point.origin");
            else                 hiddenFeatures.erase("point.origin");
        }
        syncEffFlags();
        return;
    }

    // Auto-managed planes follow graph consumption in both directions. A FeatureManager eye click
    // moves a user plane into datumPlanesUserToggled, after which neither auto-hide nor auto-show
    // touches it. Default-plane eye control is the aggregate planesUserToggled/showPlane path.
    bool datumVisibilityChanged = false;
    for (auto it = autoHiddenPlanes.begin(); it != autoHiddenPlanes.end(); ) {
        // Keep entries that are still "should be auto-hidden": consumed, OR solid-content
        // user planes that the user has not sticky-toggled. Drop everything else.
        const bool keepUser =
            userPlanes.count(*it) && !datumPlanesUserToggled.count(*it) &&
            (consumedPlanes.count(*it) || hasSolidContent);
        const bool keepDefault =
            isDefaultPlaneFid(*it) &&
            (consumedPlanes.count(*it) || (hasSolidContent && !planesUserToggled));
        if (keepUser || keepDefault) {
            ++it;
            continue;
        }
        if (userPlanes.count(*it) && !datumPlanesUserToggled.count(*it))
            datumVisibilityChanged |= hiddenFeatures.erase(*it) != 0;
        it = autoHiddenPlanes.erase(it);
    }
    for (const std::string& plane : consumedPlanes) {
        if (userPlanes.count(plane) && !datumPlanesUserToggled.count(plane)) {
            autoHiddenPlanes.insert(plane);
            datumVisibilityChanged |= hiddenFeatures.insert(plane).second;
        } else if (isDefaultPlaneFid(plane)) {
            autoHiddenPlanes.insert(plane);
        }
    }

    // Default planes hide whenever the open doc has SOLID content (a 3D feature or assembly
    // component), independent of consumption. Sketches alone leave planes visible. MUST run
    // AFTER the consumption erase loop above — that loop drops every autoHiddenPlanes entry
    // not still needed, which would undo an earlier insert for unreferenced defaults.
    // Empty / sketch-only docs: ensure the three are NOT in autoHiddenPlanes so they stay shown.
    if (!planesUserToggled) {
        static const char* kDefaultPlanes[3] = {"plane.front", "plane.top", "plane.right"};
        if (hasSolidContent) {
            for (const char* p : kDefaultPlanes) autoHiddenPlanes.insert(p);
        } else {
            for (const char* p : kDefaultPlanes) autoHiddenPlanes.erase(p);
        }
    }

    // User-created reference planes: same solid-content auto-hide as default planes / axes.
    // Without this, offset/angle planes stay drawn on solid parts and spawn a forest of
    // plane-intersection axes (axis.auto:*) that clutter the viewport. Sticky
    // datumPlanesUserToggled still wins after an FM eye click.
    for (const std::string& pid : userPlanes) {
        if (datumPlanesUserToggled.count(pid)) continue;
        if (hasSolidContent) {
            autoHiddenPlanes.insert(pid);
            if (hiddenFeatures.insert(pid).second) datumVisibilityChanged = true;
        } else if (!consumedPlanes.count(pid)) {
            autoHiddenPlanes.erase(pid);
            if (hiddenFeatures.erase(pid) != 0) datumVisibilityChanged = true;
        }
    }
    // User-created points / csys: same solid-content auto-hide as planes/axes. Sticky
    // refGeomUserToggled (set by the FM eye) keeps a forced show/hide across rebuilds.
    for (const std::string& fid : cache.referenceGeometry) {
        if (fid.empty() || fid == "point.origin") continue;
        if (refGeomUserToggled.count(fid)) continue;
        if (hasSolidContent) {
            if (hiddenFeatures.insert(fid).second) datumVisibilityChanged = true;
        } else {
            if (hiddenFeatures.erase(fid) != 0) datumVisibilityChanged = true;
        }
    }

    // Keep hiddenFeatures in step with hasSolidContent for axes so the FeatureManager eye
    // matches what the viewport draws. Per-id sticky (axesUserToggledIds) wins; axis.auto:*
    // always auto-hide on solid parts even after axesUserToggled (toggling X must not unlock
    // a forest of plane-intersection axes). Defaults still honor the aggregate flag.
    {
        auto syncAxis = [&](const std::string& id) {
            if (id.empty()) return;
            if (axesUserToggledIds.count(id)) return;
            const bool isAuto = id.rfind("axis.auto:", 0) == 0;
            if (!isAuto && axesUserToggled) return;
            if (hasSolidContent) {
                if (hiddenFeatures.insert(id).second) datumVisibilityChanged = true;
            } else {
                if (hiddenFeatures.erase(id) != 0) datumVisibilityChanged = true;
            }
        };
        syncAxis("axis.x");
        syncAxis("axis.y");
        syncAxis("axis.z");
        for (const auto& id : axisDatumIds) syncAxis(id);
    }
    // Origin: same persistent sync as axes (FM eye reads hiddenFeatures).
    if (!originUserToggled) {
        if (hasSolidContent) {
            if (hiddenFeatures.insert("point.origin").second) datumVisibilityChanged = true;
        } else {
            if (hiddenFeatures.erase("point.origin") != 0) datumVisibilityChanged = true;
        }
    }

    syncEffFlags();
    if (datumVisibilityChanged) applyDatumVisibility();
}

std::string AppState::pickLabel(const render::PickHit& h) const {
    if (!h.valid()) return std::string();
    const char* kind = "Item";
    switch (h.kind) {
        case render::PickKind::Face: kind = "Face"; break;
        case render::PickKind::Edge: kind = "Edge"; break;
        case render::PickKind::Vertex: kind = "Vertex"; break;
        default: break;
    }
    return std::string(kind) + " " + std::to_string(h.topo_id) + " of Body" +
           std::to_string(h.body_id);
}

void setActiveProjectDir(AppState& st, const std::string& absDir) {
    if (absDir.empty()) return;  // cancelled dialog → no-op
    st.workspaceDir = absDir;
    st.settings.workspaceDir = absDir;
    st.pushRecentProject(absDir);
    st.activeWorkspace = AppState::Workspace::PLM;
    ui::plmInvalidate();         // drop PLM caches → refetch against the new dir
    // The PLM hub records outside changes once when it adopts this project.
    // Scheduling it here too repeats the full working-tree scan before loading.
}
