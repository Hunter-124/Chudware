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
#include "../mesh.h"
#include "../renderer.h"

#include "../render/aicm_loader.h"
#include "../render/id_pass.h"
#include "../render/overlays.h"

#include "../ui/feature_tree.h"  // W1-B: ui::featureTreeResetTransient (clear tree statics on doc change)
#include "../ui/sketch_mode.h"   // ui::SketchMode::clearEntitySelection (clearSelection)
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

void loadSettings(Settings& s) {
    std::ifstream f(configPath());
    if (!f) {
#ifdef __EMSCRIPTEN__
        // Web: no config yet under IDBFS — still hydrate recents from the workspace.
        std::string rerr;
        auto durable = chudware::fsBackend().loadRecents(rerr);
        if (!durable.empty()) s.recentFiles = std::move(durable);
        primeRecentFilesPersistence(s.recentFiles);
#endif
        return;
    }
    try {
        nlohmann::json j;
        f >> j;
        std::string prov = j.value("provider", "zenmux");
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
        std::string model = j.value("model", "");
        if (s.provider == 3) model = llama.value("model_path", model);
        if (s.provider == 3 && model.empty()) model = kDefaultLlamaModelPath;
        if (s.provider == 6 && model.empty()) model = "grok-4.5";
        if (s.provider == 9 && model.empty()) model = "x-ai/grok-4.5-free";
        if (s.provider == 10 && model.empty()) model = fla.value("model", std::string("auto"));
        // Stale GUI ids from prior releases → the wire ids the Kimi Code endpoint now
        // accepts directly (k3 / k3-256k / kimi-for-coding). kimi-k2.7 keeps the user's
        // lighter-model pick; the rest fold onto the K3 default.
        if (s.provider == 8 && (model == "kimi-k3" || model == "kimi-for-coding" ||
                                model == "kimi-k2.7-code"))
            model = "k3";
        else if (s.provider == 8 && model == "kimi-k2.7") model = "kimi-for-coding";
        if (s.provider == 6 && model != "grok-4.5" && model.rfind("grok", 0) == 0)
            model = "grok-4.5";
        if (prov == "moonshot") {
            // Moonshot direct API (provider 5). The official wire id for Kimi K2.7 is
            // `kimi-k2.7-code` — the bare `kimi-k2.7` 404s ("Not found the model kimi-k2.7").
            // Map the GUI id up to the valid wire id; empty/kimi-for-coding fall back to K3.
            if (model.empty() || model == "kimi-for-coding") model = "kimi-k3";
            else if (model == "kimi-k2.7") model = "kimi-k2.7-code";
        }
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
        // Owner directive 2026-07-23: xAI/grok-4.5 was the default chat provider (and the target
        // of the Cursor migration just above), but most installs are NOT signed into xAI — no
        // console.x.ai API key and no completed SuperGrok/X Premium+ OAuth — so every fresh chat
        // opened straight to "xAI not configured". ZenMux's free x-ai/grok-4.5-free tier,
        // authenticated through Chudcode rather than config.json, is now the default;
        // OpenRouter's ":free" models are the documented backup.
        // Migrate a saved xAI/grok-4.5 chat provider — whether the user's own prior pick or the
        // Cursor migration above — to ZenMux, but ONLY when the user has no xAI credential of
        // their own: a deliberately-signed-in xAI user keeps xAI. One-time on load, persisted on
        // the next settings write; idempotent — once migrated (or user-repicked), Settings never
        // again satisfies "provider==xAI && model==grok-4.5" so this branch never re-fires.
        if (s.provider == 6 && model == "grok-4.5") {
            bool xaiSignedIn = s.xaiKey[0] != 0;
            if (!xaiSignedIn) {
                auto subAuth = j.value("subscription_auth", nlohmann::json::object());
                if (subAuth.is_object() &&
                    (subAuth.contains("xai") || subAuth.contains("xai-sub")))
                    xaiSignedIn = true;
            }
            if (!xaiSignedIn) {
                s.provider = 9;  // PROV_ZENMUX
                model = "x-ai/grok-4.5-free";
                copyStr(s.model, sizeof(s.model), model);
            }
        }
        copyStr(s.reasoningEffort, sizeof(s.reasoningEffort), j.value("reasoning_effort", std::string("high")));
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
            if (ag.contains("corporate_mode"))
                s.corporateMode = ag.value("corporate_mode", false);
            if (ag.contains("memory_enabled"))
                s.memoryEnabled = ag.value("memory_enabled", true);
            if (ag.contains("memory_token_budget")) {
                int b = ag.value("memory_token_budget", 400);
                s.memoryTokenBudget = (b < 50) ? 50 : (b > 4000 ? 4000 : b);
            }
        }
        if (j.contains("proactive_advisor"))
            s.proactiveAdvisor = j.value("proactive_advisor", false);
        if (j.contains("corporate_mode"))
            s.corporateMode = j.value("corporate_mode", false);
        if (j.contains("memory_enabled"))
            s.memoryEnabled = j.value("memory_enabled", true);
        if (j.contains("memory_token_budget")) {
            int b = j.value("memory_token_budget", 400);
            s.memoryTokenBudget = (b < 50) ? 50 : (b > 4000 ? 4000 : b);
        }
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
            if (backend == "off" || backend == "sharpshooter" || backend == "mnemo")
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
        s.maxTokens = j.value("max_output_tokens", 0);
        s.contextWindow = j.value("context_window", 0);
        s.systemPrompt = j.value("system_prompt", "");
        s.theme = j.value("theme", "light");
        s.orthographic = j.value("orthographic", true);
        s.displayQuality = j.value("display_quality", std::string("fine"));
        if (s.displayQuality != "draft" && s.displayQuality != "standard" &&
            s.displayQuality != "fine")
            s.displayQuality = "fine";
        s.sketchDragSnapRelations = j.value("sketch_drag_snap_relations", true);
        s.units = (j.value("units", std::string("mmgs")) == "ips") ? Units::IPS : Units::MMGS;
        s.partsGridView = j.value("parts_grid_view", false);
        s.partsSortMode = j.value("parts_sort_mode", 0);
        s.partsExpandedCats.clear();
        if (auto it = j.find("parts_expanded_categories"); it != j.end() && it->is_array())
            for (const auto& e : *it)
                if (e.is_string()) s.partsExpandedCats.push_back(e.get<std::string>());
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
        // T-G21: pinned-column vertical-collapse state.
        s.fmCollapsed = j.value("fm_collapsed", false);
        s.copilotCollapsed = j.value("copilot_collapsed", false);
        // Settings-modal progressive disclosure. Absent => false => the modal opens in its
        // readable basic form, which is the whole point of the default.
        s.showAdvanced = j.value("show_advanced", false);
        // Wave 3: File Explorer workspace + Recent files.
        s.workspaceDir = j.value("workspace_dir", std::string());
        s.recentFiles.clear();
        if (auto it = j.find("recent_files"); it != j.end() && it->is_array())
            for (const auto& e : *it)
                if (e.is_string()) s.recentFiles.push_back(e.get<std::string>());
        s.recentProjects.clear();
        if (auto it = j.find("recent_projects"); it != j.end() && it->is_array())
            for (const auto& e : *it)
                if (e.is_string()) s.recentProjects.push_back(e.get<std::string>());
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
        // A config still carrying "model_defaults_seeded" holds the list the removed first-run
        // seeding pass wrote (2026-07-23): it ran on the unfiltered ~2,350-row catalog, keyed
        // hides by bare id across providers and ranked dated snapshots above undated flagships,
        // so every model older than that day was hidden. Drop it once; saving erases the marker.
        if (!j.value("model_defaults_seeded", false))
            if (auto it = j.find("hidden_models"); it != j.end() && it->is_array())
                for (const auto& e : *it)
                    if (e.is_string()) s.hiddenModels.insert(e.get<std::string>());
        s.disabledProviders.clear();
        if (auto it = j.find("disabled_providers"); it != j.end() && it->is_array())
            for (const auto& e : *it)
                if (e.is_string()) s.disabledProviders.insert(e.get<std::string>());
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

// In-process serialization. flock is per open-file-description, so two GUI threads each
// opening the lock file would NOT exclude one another; this mutex closes that gap and is
// held for the whole read-modify-write.
std::mutex& configWriteMutex() {
    static std::mutex m;
    return m;
}
// The writer has no AppState reference (and may run on a worker thread). The UI
// consumes this once on its frame thread rather than coupling config I/O to ImGui.
std::string g_configCorruptionWarning;
}  // namespace
std::string takeConfigCorruptionWarning() {
    std::lock_guard<std::mutex> guard(configWriteMutex());
    std::string warning;
    warning.swap(g_configCorruptionWarning);
    return warning;
}


bool updateConfigJson(const std::function<void(nlohmann::json&)>& patch) {
    if (!patch) return false;
    const std::string path = configPath();
    std::lock_guard<std::mutex> guard(configWriteMutex());
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
        if (g_configCorruptionWarning.empty())
            g_configCorruptionWarning = "Settings file was corrupt; backup saved at " +
                                        backup + ". Review your settings.";
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
                          "hidden_models", "disabled_providers", "experimental", "box",
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
        j["model"] = s.model[0] ? s.model : "gpt-4o";
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
        j["corporate_mode"] = s.corporateMode;
        j["memory_backend"] = s.memoryBackend;
        j["memory_enabled"] = s.memoryBackend != "off";
        j["memory_token_budget"] = s.memoryTokenBudget;
        // Merge-overlay into agent{} so Python config helpers see the same values.
        nlohmann::json agent = nlohmann::json::object();
        if (j.contains("agent") && j["agent"].is_object()) agent = j["agent"];
        agent.erase("role_adoption");
        agent["advisor"] = (s.advisorPreference != 0);
        agent["advisor_preference"] = prefName(s.advisorPreference);
        agent["proactive_advisor"] = s.proactiveAdvisor;
        agent["corporate_mode"] = s.corporateMode;
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
    // T-G21: pinned-column vertical-collapse state.
    j["fm_collapsed"] = s.fmCollapsed;
    j["copilot_collapsed"] = s.copilotCollapsed;
    // Settings-modal progressive disclosure. Omitted while false so a default config
    // carries no trace of it.
    if (s.showAdvanced) j["show_advanced"] = true;
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
    if (!s.disabledProviders.empty())
        j["disabled_providers"] =
            std::vector<std::string>(s.disabledProviders.begin(), s.disabledProviders.end());
    // Experimental AI features. Merge-overlay: keep unknown fields in the on-disk
    // experimental block; only rewrite/clear the slots the GUI models. Omit the whole
    // block when nothing remains so a default config stays clean.
    if (s.expVisionReview) {
        expOut["vision_review"] = {
            {"enabled", true},
            {"model", s.expVisionModel[0] ? s.expVisionModel : "qwen2.5-vl-7b-instruct"},
            {"base_url", s.expVisionBaseUrl[0] ? s.expVisionBaseUrl
                                               : "http://localhost:1234/v1"},
            {"api_key", s.expVisionKey},
        };
    } else {
        expOut.erase("vision_review");
    }
    if (s.expCadSequenceMode) expOut["cad_sequence_mode"] = true;
    else expOut.erase("cad_sequence_mode");
    if (!expOut.empty()) j["experimental"] = std::move(expOut);
}

void saveSettings(const Settings& s) {
    // Locked + atomic via the shared config lock, so a concurrent Python/engine write can
    // neither clobber us nor be clobbered by us between our read and our write.
    updateConfigJson([&s](nlohmann::json& j) { applyGuiOwnedSettings(j, s); });
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

bool AppState::convRunning(const std::string& convId) const {
    auto it = runs.find(convId);
    return it != runs.end() && it->second && it->second->running.load();
}

bool AppState::docLockedByAgent(const std::string& docId) const {
    if (docId.empty()) return false;
    for (const DocTab& t : docTabs) {
        if (t.docId != docId) continue;
        if (t.lastForeignWriteAt == std::chrono::steady_clock::time_point{}) return false;
        const auto now = std::chrono::steady_clock::now();
        if (std::chrono::duration<double>(now - t.lastForeignWriteAt).count() < kAgentLockGraceS)
            return true;
        return anyRunActive() && t.lastForeignWriteAt >= runsIdleAt;
    }
    return false;
}

bool AppState::activeDocLockedByAgent() const {
    return docLockedByAgent(docMirror.docId());
}

std::string AppState::agentLockReason(const std::string& docId) const {
    if (!docLockedByAgent(docId)) return std::string();
    return anyRunActive()
               ? "An agent is editing this part. Editing is locked until it finishes (Stop it to "
                 "take over)."
               : "An agent just edited this part. Editing unlocks in a few seconds.";
}

std::string AppState::activeConvId() const {
    if (activeConv < 0 || activeConv >= (int)conversations.size()) return std::string();
    return conversations[activeConv].id;
}

// ---------------- AppState methods (formerly run_gui lambdas) ----------------
void AppState::addLog(const std::string& s) {
    {
        std::lock_guard<std::mutex> lk(logMtx);
        log.push_back(s);
        if (log.size() > 500) log.erase(log.begin());
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
static bool activeCadDocMatchesMirror(const AppState& st) {
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return false;
    const auto& t = st.docTabs[st.activeTab];
    if (t.kind != AppState::DocTab::TabKind::Part &&
        t.kind != AppState::DocTab::TabKind::Assembly) return false;
    if (t.docId.empty() || !st.docMirror.hasDoc()) return false;
    return t.docId == st.docMirror.docId();
}

core::OpsResponse AppState::docUndo() {
    if (!activeCadDocMatchesMirror(*this)) {
        core::OpsResponse r;
        r.error_code = "no_doc";
        r.error_message = "no active CAD document";
        return r;
    }
    if (docLockedByAgent(docMirror.docId())) {
        core::OpsResponse r;
        r.error_code = "agent_edit_locked";
        r.error_message = agentLockReason(docMirror.docId());
        toast(ui::ToastLevel::Warn, r.error_message);
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
    if (!activeCadDocMatchesMirror(*this)) {
        core::OpsResponse r;
        r.error_code = "no_doc";
        r.error_message = "no active CAD document";
        return r;
    }
    if (docLockedByAgent(docMirror.docId())) {
        core::OpsResponse r;
        r.error_code = "agent_edit_locked";
        r.error_message = agentLockReason(docMirror.docId());
        toast(ui::ToastLevel::Warn, r.error_message);
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
    return;
#endif
    workerStarted = worker.start(werr);
    if (workerStarted) {
        status = "Ready.";
        statusKind = 0;
        addLog("[ok] kernel worker ready");
        return;
    }
    addLog("[err] worker: " + werr);
    // If the managed worker is unavailable, the in-process C++ core is the
    // cross-platform fallback (doc/agent/sim ops route through cpp_core). This
    // is also what makes a clean Windows CI checkout able to run headless shots
    // without provisioning the Python runtime first.
    //
    // NOTE: when CHUDWARE_ENGINE_DAEMON is unset/1 (default), engine::call goes
    // to chudware-hostd, which dlopens its OWN libchudware. The path logged here
    // is the GUI process's in-proc load (fallback / parity); hostd's path is in
    // its stderr "[cpp_core] loaded ..." line and in daemon hello
    // engine_build_id. Stale hostd holding an old library is the classic trap:
    // stop it after a core rebuild, or wait for hostd's mtime self-drain.
    {
        std::string lerr;
        if (core::cpp_core().ensureLoaded(lerr)) {
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
            return;
        }
        addLog("[..] libchudware not loaded: " + lerr);
    }
    if (!autoSetupTried && !setupRunning.load()) {
        autoSetupTried = true;
        addLog("[..] attempting automatic runtime setup...");
        startSetup(runtimeReady());  // force a rebuild if a broken venv is present
    } else {
        status = "Runtime needs setup";
        statusKind = 2;
    }
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

// Convert one AICM body's tessellation to a shaded Mesh (for the studio pass + camera bounds).
// Faithfully reuses the AICM positions/normals/indices and the per-EDGE polylines as feature
// edges, so the shaded view matches exactly what the id pass picks.
static Mesh meshFromBody(const render::BodyMesh& b) {
    Mesh m;
    m.positions = b.pos;
    m.normals = b.nrm;
    m.indices = b.idx;
    // feature edges: emit every segment of every edge polyline as a line-list pair
    for (const auto& es : b.edges) {
        for (uint32_t i = 0; i + 1 < es.pt_count; ++i) {
            const float* a = &b.edge_pts[(size_t)(es.first_pt + i) * 3];
            const float* c = &b.edge_pts[(size_t)(es.first_pt + i + 1) * 3];
            m.edgeLines.insert(m.edgeLines.end(), {a[0], a[1], a[2], c[0], c[1], c[2]});
        }
    }
    // bounds from positions
    Vec3 mn(1e30f, 1e30f, 1e30f), mx(-1e30f, -1e30f, -1e30f);
    for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
        float x = b.pos[i], y = b.pos[i + 1], z = b.pos[i + 2];
        if (x < mn.x) mn.x = x; if (y < mn.y) mn.y = y; if (z < mn.z) mn.z = z;
        if (x > mx.x) mx.x = x; if (y > mx.y) mx.y = y; if (z > mx.z) mx.z = z;
    }
    m.bbMin = mn; m.bbMax = mx; m.center = (mn + mx) * 0.5f;
    m.radius = std::max(0.001f, length(mx - mn) * 0.5f);
    return m;
}

static std::vector<Renderer::PartMesh> rendererPartsFromDoc(
    const AppState& st, const render::DocMesh& doc) {
    std::vector<Renderer::PartMesh> parts;
    parts.reserve(doc.bodies.size());
    for (const auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        Renderer::PartMesh pm;
        pm.mesh = meshFromBody(b);
        pm.color[0] = b.color[0];
        pm.color[1] = b.color[1];
        pm.color[2] = b.color[2];
        pm.metallic = b.metallic;
        pm.roughness = b.roughness;
        pm.appearance_regions = b.appearance_regions;
        pm.body_id = b.body_id;
        pm.faces.reserve(b.faces.size());
        for (const auto& fr : b.faces)
            pm.faces.push_back({fr.face_id, fr.first_tri, fr.tri_count});
        const render::ScalarField* chosen = nullptr;
        for (const auto& sc : doc.scalars) {
            if (sc.body_id != b.body_id || sc.values.size() != b.n_verts) continue;
            if (sc.name == st.activeScalarField) {
                chosen = &sc;
                break;
            }
            if (!chosen) chosen = &sc;
        }
        if (chosen) {
            pm.scalar = chosen->values;
            pm.scalarMin = chosen->vmin;
            pm.scalarMax = chosen->vmax;
        }
        for (const auto& df : doc.displacements) {
            if (df.body_id == b.body_id && df.vec.size() == (size_t)b.n_verts * 3) {
                pm.disp = df.vec;
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

// T-PERF3: dedicated mesh-loader thread. Disk parse of AICM is the multi-100ms hit on big
// docs; it runs here. GL upload + AppState cache commit stay on the GL thread in
// drainMeshLoads / commitDocMeshFromParsed.
namespace {
struct MeshLoadJob {
    uint64_t gen = 0;
    std::string path;
    bool renderOnly = false;
    bool cacheAsModel = true;
    bool cacheOnly = false;
    std::string docId;
    int revision = -1;
    std::chrono::steady_clock::time_point queuedAt;
    bool ok = false;
    std::string err;
    render::DocMesh doc;
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
        {
            std::lock_guard<std::mutex> lk(g_meshMtx);
            g_meshReady.push_back(std::move(job));
        }
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
}  // namespace

// T-PERF3/10: join the loader before process exit. A joinable static std::thread
// dtor calls std::terminate ("terminate called without an active exception") —
// seen as a full-app crash when teardown races vendor-browser / CEF exit paths.
void AppState::stopMeshLoader() { stopMeshLoaderThread(); }

// Commit a parsed DocMesh onto the GL-side state (setMeshes / id-pass / pick caches).
// Shared by the sync loadDocMesh path and drainMeshLoads. Requires GL context.
static bool commitDocMeshFromParsed(AppState& st, render::DocMesh& doc, std::string& err,
                                    bool renderOnly, bool shadedAlreadyBound = false);

bool AppState::loadDocMesh(const std::string& aicmPath, std::string& err, bool renderOnly,
                          bool cacheAsModel) {
    if (!renderer || !idPass || !overlays) { err = "render objects not initialized"; return false; }
    render::DocMesh doc;
    if (!render::loadAicmFile(aicmPath, doc, err)) return false;
    if (!cacheAsModel) renderer->parkActiveMeshes();
    if (!commitDocMeshFromParsed(*this, doc, err, renderOnly)) return false;
    if (!renderOnly && cacheAsModel && docMirror.hasDoc()) {
        renderer->setActiveMeshIdentity(docMirror.docId(), docMirror.revision());
        if (DocSession* s = activeSession()) {
            s->cachedAicmPath = aicmPath;
            s->cachedRevision = docMirror.revision();
            s->cachedMirror = docMirror.snapshot();
            s->cachedDocMesh = std::make_shared<render::DocMesh>(std::move(doc));
        }
    }
    return true;
}

void AppState::loadDocMeshAsync(const std::string& aicmPath, bool renderOnly, bool cacheAsModel) {
    if (aicmPath.empty()) return;
    if (!renderer || !idPass || !overlays) return;
    ensureMeshLoader();
    const uint64_t gen = ++meshGen_;
    {
        std::lock_guard<std::mutex> lk(g_meshMtx);
        // Latest visible load wins, but pushed background-cache jobs are independent.
        g_meshQueue.erase(
            std::remove_if(g_meshQueue.begin(), g_meshQueue.end(),
                           [](const MeshLoadJob& j) { return !j.cacheOnly; }),
            g_meshQueue.end());
        MeshLoadJob job;
        job.gen = gen;
        job.path = aicmPath;
        job.renderOnly = renderOnly;
        job.cacheAsModel = cacheAsModel;
        job.queuedAt = std::chrono::steady_clock::now();
        g_meshQueue.push_back(std::move(job));
    }
    g_meshCv.notify_one();
}

void AppState::cacheBackgroundDocMeshAsync(const std::string& docId, int revision,
                                           const std::string& aicmPath) {
    if (docId.empty() || revision < 0 || aicmPath.empty() || !renderer) return;
    ensureMeshLoader();
    {
        std::lock_guard<std::mutex> lk(g_meshMtx);
        g_meshQueue.erase(
            std::remove_if(g_meshQueue.begin(), g_meshQueue.end(),
                           [&](const MeshLoadJob& j) {
                               return j.cacheOnly && j.docId == docId;
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
    // Inactive pushed revisions prewarm their parsed+GPU caches without touching the viewport.
    // Cap the GPU uploads per frame: with an AI swarm pushing revisions to dozens of mirror
    // tabs, committing every parsed prewarm in one drain stacked that many full-mesh uploads
    // into a single frame and visibly hitched the viewport. Deferred jobs go back to the
    // ready queue (front, order kept) and commit on the following frames.
    constexpr int kMaxPrewarmCommitsPerFrame = 2;
    int prewarmCommits = 0;
    std::vector<MeshLoadJob> deferred;
    for (auto& j : ready) {
        if (!j.cacheOnly) continue;
        if (!j.ok) {
            addLog("[daemon] background mesh parse failed for " + j.docId + ": " + j.err);
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
        if (!cachedTab) continue;  // tab closed or a newer pushed revision superseded this parse
        if (prewarmCommits >= kMaxPrewarmCommitsPerFrame) {
            deferred.push_back(std::move(j));
            continue;
        }
        ++prewarmCommits;
        DocSession* session = cachedTab->session.get();
        const auto parts = rendererPartsFromDoc(*this, j.doc);
        renderer->cacheMeshes(j.docId, j.revision, parts);
        session->cachedDocMesh = std::make_shared<render::DocMesh>(std::move(j.doc));
        cachedTab->meshCacheInvalidated = false;
        const double ms = std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - j.queuedAt).count();
        const std::string perf = "[perf] live_mesh_prewarm doc=" + j.docId + " rev=" +
            std::to_string(j.revision) + " total=" + std::to_string(ms) + "ms";
        addLog(perf);
        std::fprintf(stderr, "%s\n", perf.c_str());
        std::fflush(stderr);
    }
    if (!deferred.empty()) {
        std::lock_guard<std::mutex> lk(g_meshMtx);
        g_meshReady.insert(g_meshReady.begin(),
                           std::make_move_iterator(deferred.begin()),
                           std::make_move_iterator(deferred.end()));
    }
    // Apply only the newest visible job that still matches meshGen_.
    MeshLoadJob* best = nullptr;
    for (auto& j : ready) {
        if (!j.cacheOnly && j.gen == cur) best = &j;
    }
    if (!best) return;
    if (!best->ok) {
        addLog("[err] loadDocMeshAsync: " + best->err);
        return;
    }
    std::string err;
    if (!best->cacheAsModel) renderer->parkActiveMeshes();
    if (!commitDocMeshFromParsed(*this, best->doc, err, best->renderOnly)) {
        if (!err.empty()) addLog("[err] loadDocMeshAsync commit: " + err);
        return;
    }
    if (!best->renderOnly && best->cacheAsModel && docMirror.hasDoc()) {
        renderer->setActiveMeshIdentity(docMirror.docId(), docMirror.revision());
        if (DocSession* s = activeSession()) {
            s->cachedAicmPath = best->path;
            s->cachedRevision = docMirror.revision();
            s->cachedMirror = docMirror.snapshot();
            s->cachedDocMesh = std::make_shared<render::DocMesh>(std::move(best->doc));
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
        std::fprintf(stderr, "%s\n", perf.c_str());
        std::fflush(stderr);
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
        std::vector<Renderer::PartMesh> rpms;
        for (const auto& b : doc.bodies) {
            if (b.pos.empty()) continue;
            Renderer::PartMesh pm;
            pm.mesh = meshFromBody(b);
            pm.color[0] = b.color[0]; pm.color[1] = b.color[1]; pm.color[2] = b.color[2];
            pm.metallic = b.metallic; pm.roughness = b.roughness;
            pm.appearance_regions = b.appearance_regions;
            pm.body_id = b.body_id;
            pm.faces.reserve(b.faces.size());
            for (const auto& fr : b.faces)
                pm.faces.push_back({fr.face_id, fr.first_tri, fr.tri_count});
            rpms.push_back(std::move(pm));
        }
        st.renderer->setMeshes(rpms);
        return true;
    }
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
    std::vector<Renderer::PartMesh> pms;
    std::vector<uint32_t> newBodyIds;  // AICM body id per st.renderer slot (parallels pms)
    st.docMeshHasScalar = false;
    st.docMeshHasDisp = false;
    for (const auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        newBodyIds.push_back(b.body_id);
        Renderer::PartMesh pm;
        if (!shadedAlreadyBound) {
            pm.mesh = meshFromBody(b);
            pm.color[0] = b.color[0]; pm.color[1] = b.color[1]; pm.color[2] = b.color[2];
            pm.metallic = b.metallic; pm.roughness = b.roughness;
            pm.appearance_regions = b.appearance_regions;
            // Face runs share the unmodified body index buffer for edit-preview isolation.
            pm.body_id = b.body_id;
            pm.faces.reserve(b.faces.size());
            for (const auto& fr : b.faces)
                pm.faces.push_back({fr.face_id, fr.first_tri, fr.tri_count});
        }
        // Prefer the selected result field; fall back to the first valid field for this body.
        const render::ScalarField* chosen = nullptr;
        for (const auto& sc : doc.scalars) {
            if (sc.body_id != b.body_id || sc.values.size() != b.n_verts) continue;
            if (sc.name == st.activeScalarField) { chosen = &sc; break; }
            if (!chosen) chosen = &sc;
        }
        if (chosen) {
            st.docMeshHasScalar |= !chosen->values.empty();
            if (!shadedAlreadyBound) {
                pm.scalar = chosen->values;
                pm.scalarMin = chosen->vmin;
                pm.scalarMax = chosen->vmax;
            }
        }
        // First displacement field for this body (FEA deformed shape).
        for (const auto& df : doc.displacements) {
            if (df.body_id == b.body_id && df.vec.size() == (size_t)b.n_verts * 3) {
                if (!df.vec.empty()) {
                    st.docMeshHasDisp = true;
                    st.dispScaleHint = df.scale_hint;
                }
                if (!shadedAlreadyBound) {
                    pm.disp = df.vec;
                    pm.dispScaleHint = df.scale_hint;
                }
                break;
            }
        }
        if (!shadedAlreadyBound) pms.push_back(std::move(pm));
    }
    // C48: unconditional — pms can be empty when every body has no triangles; keeping the
    // previous meshes would desync the viewport from the document (same stale-mesh bug as
    // the zero-bodies case above).
    if (!shadedAlreadyBound) st.renderer->setMeshes(pms);


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

    // C26-C: build per-face normal + centroid cache while the DocMesh is still in memory.
    // For each face, accumulate area-weighted triangle normals and centroids.
    st.faceNormalCache.clear();
    for (const auto& b : doc.bodies) {
        if (b.pos.empty() || b.idx.empty() || b.faces.empty()) continue;
        for (const auto& fr : b.faces) {
            float nx = 0, ny = 0, nz = 0;  // accumulated area-weighted normal
            float cx = 0, cy = 0, cz = 0;  // accumulated area-weighted centroid
            float totalArea = 0;
            for (uint32_t t = 0; t < fr.tri_count; ++t) {
                size_t base = (size_t)(fr.first_tri + t) * 3;
                if (base + 2 >= b.idx.size()) break;
                uint32_t i0 = b.idx[base], i1 = b.idx[base + 1], i2 = b.idx[base + 2];
                // vertex positions
                const float* p0 = &b.pos[(size_t)i0 * 3];
                const float* p1 = &b.pos[(size_t)i1 * 3];
                const float* p2 = &b.pos[(size_t)i2 * 3];
                // edge vectors
                float e1x = p1[0]-p0[0], e1y = p1[1]-p0[1], e1z = p1[2]-p0[2];
                float e2x = p2[0]-p0[0], e2y = p2[1]-p0[1], e2z = p2[2]-p0[2];
                // cross product (area-weighted normal, magnitude = 2*area)
                float crx = e1y*e2z - e1z*e2y;
                float cry = e1z*e2x - e1x*e2z;
                float crz = e1x*e2y - e1y*e2x;
                float area = std::sqrt(crx*crx + cry*cry + crz*crz);
                nx += crx; ny += cry; nz += crz;
                float triCx = (p0[0]+p1[0]+p2[0]) * (1.0f/3.0f);
                float triCy = (p0[1]+p1[1]+p2[1]) * (1.0f/3.0f);
                float triCz = (p0[2]+p1[2]+p2[2]) * (1.0f/3.0f);
                cx += triCx * area; cy += triCy * area; cz += triCz * area;
                totalArea += area;
            }
            // normalize normal; normalize centroid by total area
            float len = std::sqrt(nx*nx + ny*ny + nz*nz);
            if (len < 1e-12f) continue;
            nx /= len; ny /= len; nz /= len;
            if (totalArea > 1e-12f) { cx /= totalArea; cy /= totalArea; cz /= totalArea; }
            uint64_t key = ((uint64_t)b.body_id << 32) | (uint64_t)fr.face_id;
            st.faceNormalCache[key] = {nx, ny, nz, cx, cy, cz};
        }
    }

    // C27-A: per-edge centroid cache (mean of each edge's polyline points). Mirrors the face-cache
    // keying so callout anchors can resolve PickKind::Edge topo ids -> a world point.
    st.edgeCentroidCache.clear();
    for (const auto& b : doc.bodies) {
        if (b.edges.empty() || b.edge_pts.empty()) continue;
        for (const auto& es : b.edges) {
            if (es.pt_count == 0) continue;
            double cx = 0, cy = 0, cz = 0;
            uint32_t n = 0;
            for (uint32_t p = 0; p < es.pt_count; ++p) {
                size_t base = (size_t)(es.first_pt + p) * 3;
                if (base + 2 >= b.edge_pts.size()) break;
                cx += b.edge_pts[base]; cy += b.edge_pts[base + 1]; cz += b.edge_pts[base + 2];
                ++n;
            }
            if (n == 0) continue;
            uint64_t key = ((uint64_t)b.body_id << 32) | (uint64_t)es.edge_id;
            st.edgeCentroidCache[key] = {(float)(cx / n), (float)(cy / n), (float)(cz / n)};
        }
    }

    // W1-B (item 8): per-body and per-face axis-aligned bounding-box caches for Zoom-to-Selection.
    // Built from the same in-memory DocMesh positions; consumed by a later wave's zoom-to-fit.
    st.bodyBBoxCache.clear();
    st.faceBBoxCache.clear();
    for (const auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        // Body bbox: sweep all positions.
        float bmn[3] = {1e30f, 1e30f, 1e30f}, bmx[3] = {-1e30f, -1e30f, -1e30f};
        for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
            for (int c = 0; c < 3; ++c) {
                float v = b.pos[i + c];
                if (v < bmn[c]) bmn[c] = v;
                if (v > bmx[c]) bmx[c] = v;
            }
        }
        st.bodyBBoxCache[b.body_id] = {bmn[0], bmn[1], bmn[2], bmx[0], bmx[1], bmx[2]};
        // Per-face bbox: sweep the vertices referenced by each face's triangle range.
        if (b.idx.empty() || b.faces.empty()) continue;
        for (const auto& fr : b.faces) {
            if (fr.tri_count == 0) continue;
            float fmn[3] = {1e30f, 1e30f, 1e30f}, fmx[3] = {-1e30f, -1e30f, -1e30f};
            bool any = false;
            for (uint32_t t = 0; t < fr.tri_count; ++t) {
                size_t base = (size_t)(fr.first_tri + t) * 3;
                if (base + 2 >= b.idx.size()) break;
                for (int k = 0; k < 3; ++k) {
                    uint32_t vi = b.idx[base + k];
                    const float* p = &b.pos[(size_t)vi * 3];
                    for (int c = 0; c < 3; ++c) {
                        if (p[c] < fmn[c]) fmn[c] = p[c];
                        if (p[c] > fmx[c]) fmx[c] = p[c];
                    }
                    any = true;
                }
            }
            if (!any) continue;
            uint64_t key = ((uint64_t)b.body_id << 32) | (uint64_t)fr.face_id;
            st.faceBBoxCache[key] = {fmn[0], fmn[1], fmn[2], fmx[0], fmx[1], fmx[2]};
        }
    }

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
    if (!commitDocMeshFromParsed(*this, *s.cachedDocMesh, err, false, gpuBound)) return false;
    renderer->setActiveMeshIdentity(s.cachedMirror.docId, s.cachedRevision);
    return true;
}

void AppState::unloadDocMesh() {
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
    // frame 0. (Replaces the old T-BL37 hidden-by-default seed.)
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

// Exit component-edit: persist the edited component to disk, then switch back to the assembly with a
// FORCED full rebuild so the edit actually shows.
//
// Why a forced rebuild (not the cheap cached switch): the assembly re-embeds each component by reading
// its `.step+` OFF DISK, keyed by abspath:mtime (core/doc/assembly.cpp load_component → component_cache_key
// → load_bodies). The cheap tab-switch is keyed on the ASSEMBLY doc's revision, which an in-context
// component edit does NOT bump — so a plain switch-back would re-serve the pre-edit mesh. Two things are
// therefore required, both only on this discrete exit action (the normal fast tab-switch path is untouched):
//   1) SAVE the component doc to its source `.step+` first, bumping the file mtime so the assembly's
//      abspath:mtime component cache misses and re-imports the edited geometry. The in-context edit lives
//      only in the worker's in-memory doc until this save.
//   2) After the save lands, invalidate every assembly-tab cache layer so switchToTab takes the
//      asynchronous doc_rebuild path (a real re-embed + re-tessellate). doc_save is async, so the
//      rebuild MUST be chained from the save's success callback — firing it sequentially would race
//      the rebuild ahead of the on-disk write.
void AppState::exitComponentEdit() {
    if (!componentEdit.active) return;
    const std::string returnDocId  = componentEdit.assemblyDocId;
    const std::string sourcePath   = componentEdit.sourcePath;   // component's resolved abs .step+ path
    // The component's part-doc is the ACTIVE doc at exit (we haven't switched back yet).
    const std::string componentDocId = docMirror.docId();
    // Reset context BEFORE the switch so any repaint during switchToTab sees us out of component-edit.
    componentEdit = ComponentEditCtx{};
    if (renderer) renderer->clearGhost();

    // Switch back to the assembly with its session cache invalidated so switchToTab forces a full
    // async doc_rebuild (re-reads the edited component from disk). Factored so both the post-save
    // path and the fallback (no source path / no worker) can call it.
    auto switchBackForced = [this, returnDocId]() {
        if (returnDocId.empty()) return;
        for (int i = 0; i < (int)docTabs.size(); ++i) {
            if (docTabs[i].docId != returnDocId) continue;
            if (docTabs[i].session) {
                docTabs[i].session->cachedRevision = -1;   // force needs_rebuild (see doc_mirror.cpp)
                docTabs[i].session->cachedAicmPath.clear();
                docTabs[i].session->cachedMirror = {};
                docTabs[i].session->cachedDocMesh.reset();
            }
            if (renderer) renderer->discardCachedMeshes(returnDocId);
            switchToTab(i);
            break;
        }
    };

    // Save the component to disk first (bump mtime), then force the assembly rebuild in the callback.
    // Fall back to a bare forced switch when we can't save (no anchored source path or no worker):
    // the rebuild is still correct if the component happened to be saved by hand during the edit.
    if (!sourcePath.empty() && !componentDocId.empty() && workerStarted) {
        const std::string savePath = sourcePath;
        const std::string saveDocId = componentDocId;
        ++rebuildsInFlight;
        rpc.call("doc_save",
                 nlohmann::json{{"doc_id", saveDocId}, {"path", savePath}},
                 [this, switchBackForced, saveDocId](const core::RpcResult& r) {
                     --rebuildsInFlight;
                     if (!r.ok) {
                         addLog("[asm] exit-edit: component save failed: " + r.err);
                         toast(ui::ToastLevel::Warn,
                               "Component didn't save — assembly may show the old model");
                     } else {
                         addLog("[asm] exit-edit: component saved (" + saveDocId +
                                "), forcing assembly rebuild");
                     }
                     switchBackForced();   // rebuild regardless — a save error shouldn't strand us
                 }, 120000);
        return;
    }
    switchBackForced();
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

// True when the doc has at least one solid/3D-producing feature (or assembly components).
// Sketches and pure reference datums do NOT count — refs stay visible after the first sketch
// and only auto-hide once a real solid feature (extrude/revolve/fillet/…) exists.
static bool treeHasSolidContent(const nlohmann::json& tree) {
    if (!tree.is_object()) return false;
    auto ci = tree.find("components");
    if (ci != tree.end() && ci->is_array() && !ci->empty()) return true;
    auto fi = tree.find("features");
    if (fi == tree.end() || !fi->is_array()) return false;
    for (const auto& f : *fi) {
        if (!f.is_object()) continue;
        if (f.value("suppressed", false)) continue;
        const std::string type = f.value("type", std::string());
        if (type.empty()) continue;
        if (!isReferenceOnlyFeature(type)) return true;
    }
    return false;
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

    // Shared hasSolidContent signal (mirrors viewport origin/axis/plane auto-hide): a solid/3D
    // feature or any assembly component means references should auto-hide. Sketches alone do not.
    const bool hasSolidContent = treeHasSolidContent(tree);
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
    auto fi = tree.find("features");

    if (isAsm) {
        // Assembly: re-derive the per-component hide mask from document.json. The engine builds one
        // body per NON-suppressed component, 1-based ordinal == body_id (assembly.cpp).
        auto ci = tree.find("components");
        if (ci != tree.end() && ci->is_array()) {
            uint32_t ordinal = 0;
            for (const auto& c : *ci) {
                if (!c.is_object()) continue;
                if (c.value("suppressed", false)) continue;
                ++ordinal;
                uint8_t vis = c.value("hidden", false) ? 0 : 1;
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

    // Build the ACTIVE graph only: suppressed and rolled-back features do not consume anything.
    std::unordered_map<std::string, const nlohmann::json*> activeFeatures;
    std::unordered_set<std::string> userPlanes;
    std::string rollbackAfter;
    auto rollback = tree.find("rollback_after");
    if (rollback != tree.end() && rollback->is_string())
        rollbackAfter = rollback->get<std::string>();
    bool pastRollback = false;
    for (const auto& f : *fi) {
        if (!f.is_object() || pastRollback) continue;
        const std::string fid = f.value("id", std::string());
        if (!fid.empty() && !f.value("suppressed", false)) {
            activeFeatures.emplace(fid, &f);
            if (isDatumPlaneType(f.value("type", std::string()))) userPlanes.insert(fid);
        }
        if (!rollbackAfter.empty() && fid == rollbackAfter) pastRollback = true;
    }

    // Start only at actual model features, then walk their feature dependencies. This is the key
    // distinction between "a sketch/ref-plane exists" and "a downstream 3D feature consumed it".
    std::unordered_set<std::string> consumedPlanes;
    for (const auto& featureEntry : activeFeatures) {
        const nlohmann::json* root = featureEntry.second;
        if (isReferenceOnlyFeature(root->value("type", std::string()))) continue;

        std::unordered_set<std::string> pending;
        auto refs = root->find("refs");
        if (refs != root->end())
            collectFeatureRefs(*refs, activeFeatures, pending, consumedPlanes);

        std::unordered_set<std::string> visited;
        while (!pending.empty()) {
            auto next = pending.begin();
            std::string depId = *next;
            pending.erase(next);
            if (!visited.insert(depId).second) continue;

            auto dep = activeFeatures.find(depId);
            if (dep == activeFeatures.end()) continue;
            const nlohmann::json& feature = *dep->second;
            if (isDatumPlaneType(feature.value("type", std::string())))
                consumedPlanes.insert(depId);
            auto depRefs = feature.find("refs");
            if (depRefs != feature.end())
                collectFeatureRefs(*depRefs, activeFeatures, pending, consumedPlanes);
        }
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
    for (const auto& featureEntry : activeFeatures) {
        const nlohmann::json& f = *featureEntry.second;
        const std::string ty = f.value("type", std::string());
        if (ty != "point" && ty != "datum_point" && ty != "ref_point" &&
            ty != "csys" && ty != "coordinate_system" && ty != "ref_csys")
            continue;
        const std::string& fid = featureEntry.first;
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
