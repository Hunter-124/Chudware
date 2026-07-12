// AI-CAD shared application state + data model — implementations (extracted from gui_app.cpp).
#include <glad/gl.h>
//
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "stb_image.h"
#include "stb_image_resize2.h"

#include <algorithm>
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

#include "app_state.h"

#include "../bootstrap.h"
#include "cpp_core.h"  // in-process C++ core (libaicad) — the backend when no Python runtime
#include "../mesh.h"
#include "../renderer.h"

#include "../render/aicm_loader.h"
#include "../render/id_pass.h"
#include "../render/overlays.h"

#include "../ui/feature_tree.h"  // W1-B: ui::featureTreeResetTransient (clear tree statics on doc change)
#include "../ui/sketch_mode.h"   // ui::SketchMode::clearEntitySelection (clearSelection)

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
#ifdef _WIN32
    const char* a = std::getenv("APPDATA");
    return std::string(a ? a : ".");
#else
    // Parent of the AI-CAD data dir (callers append "/AI-CAD/..."): XDG data home, else
    // ~/.local/share — matching bootstrap.cpp's appdataRoot() (~/.local/share/AI-CAD).
    if (const char* x = std::getenv("XDG_DATA_HOME")) { if (*x) return std::string(x); }
    if (const char* h = std::getenv("HOME")) { if (*h) return std::string(h) + "/.local/share"; }
    return ".";
#endif
}
// Billet deep-rename, alias/read-both rule (mirrors engine/paths.py active_root() and
// core/agent/paths.cpp pick_app_dir() EXACTLY): a machine with an existing legacy AI-CAD
// data dir keeps using it wholesale (it holds the live runtime venv/config — venvs do not
// survive relocation) UNLESS the new Billet dir already holds a real install marker
// (config.json); fresh installs get Billet. Never moves or deletes the old dir.
std::string appdataAppDir() {
    static const std::string cached = [] {
        namespace fs = std::filesystem;
        const fs::path base = appdataDir();
        const fs::path legacy = base / "AI-CAD";
        const fs::path neu = base / "Billet";
        std::error_code ec;
        std::string chosen = (fs::exists(legacy, ec) && !fs::exists(neu / "config.json", ec))
                                 ? legacy.string()
                                 : neu.string();
        std::fprintf(stderr, "[billet] data dir: %s\n", chosen.c_str());
        return chosen;
    }();
    return cached;
}
std::string configPath() { return appdataAppDir() + "/config.json"; }

// Default workspace dir under Documents — same alias rule: an existing Documents/AI-CAD
// keeps being used; otherwise the default is Documents/Billet (created on first use by
// the File Explorer). Falls back to the appdata base when no home/Documents resolves.
std::string defaultWorkspaceDir() {
    namespace fs = std::filesystem;
#ifdef _WIN32
    const char* up = std::getenv("USERPROFILE");
    const fs::path base = up && *up ? fs::path(up) / "Documents" : fs::path(appdataDir());
#else
    const char* home = std::getenv("HOME");
    const fs::path base = home && *home ? fs::path(home) / "Documents" : fs::path(appdataDir());
#endif
    std::error_code ec;
    const fs::path legacy = base / "AI-CAD";
    if (fs::exists(legacy, ec)) return legacy.string();
    return (base / "Billet").string();
}
const char* kDefaultLlamaModelPath = "%APPDATA%\\AI-CAD\\models\\model.gguf";

// User-provided Gemini key, seeded as the out-of-box default so the copilot works on first
// launch. Replaceable in Settings → AI. (Stored plaintext in %APPDATA%/AI-CAD/config.json,
// same as every other provider key.)
const char* kSeededGeminiKey = "AQ.Ab8RN6KgQVaemw7y3lReGzhLlD4VpUnyCYCxgT7ABxbNe1QKbA";

static void copyStr(char* dst, size_t cap, const std::string& v) {
    std::strncpy(dst, v.c_str(), cap - 1);
    dst[cap - 1] = '\0';
}

// Canonical config.json "appearance.glass" group keys — index == (size_t)ui::GlassGroup.
// Shared by loadSettings/saveSettings so the schema can never diverge between the two.
static const char* kGlassGroupKey[] = {"global", "feature_manager", "copilot", "hud"};

void loadSettings(Settings& s) {
    // Seed the shipped Gemini key by default; an on-disk config overrides it below. Done
    // before the file check so a fresh install (no config.json yet) still has a working key.
    if (!s.geminiKey[0]) copyStr(s.geminiKey, sizeof(s.geminiKey), kSeededGeminiKey);
    std::ifstream f(configPath());
    if (!f) return;
    try {
        nlohmann::json j;
        f >> j;
        std::string prov = j.value("provider", "gemini");
        auto oai = j.value("openai", nlohmann::json::object());
        auto llama = j.value("llamacpp", nlohmann::json::object());
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
        else if (hasBase) s.provider = baseLoopback ? 2 : 5;  // 2=Local, 5=Cloud OpenAI-compatible
        else s.provider = 1;                                   // 1=OpenAI cloud (api.openai.com)
        std::string model = j.value("model", "");
        if (s.provider == 3) model = llama.value("model_path", model);
        if (s.provider == 3 && model.empty()) model = kDefaultLlamaModelPath;
        copyStr(s.model, sizeof(s.model), model);
        std::string activeKey = j.value("api_key", "");
        copyStr(s.apiKey, sizeof(s.apiKey), activeKey);

        // Per-provider keys (config.json "keys"), so the picker can hide unkeyed providers.
        auto keys = j.value("keys", nlohmann::json::object());
        copyStr(s.geminiKey, sizeof(s.geminiKey), keys.value("gemini", std::string()));
        copyStr(s.anthropicKey, sizeof(s.anthropicKey), keys.value("anthropic", std::string()));
        copyStr(s.openaiKey, sizeof(s.openaiKey), keys.value("openai", std::string()));
        copyStr(s.cloudKey, sizeof(s.cloudKey), keys.value("cloud_compat", std::string()));
        // Back-compat: fold the active provider's top-level api_key into its per-provider slot.
        if (!activeKey.empty()) {
            if (prov == "gemini" && !s.geminiKey[0]) copyStr(s.geminiKey, sizeof(s.geminiKey), activeKey);
            else if (prov == "anthropic" && !s.anthropicKey[0]) copyStr(s.anthropicKey, sizeof(s.anthropicKey), activeKey);
            else if (prov == "openai" && !hasBase && !s.openaiKey[0]) copyStr(s.openaiKey, sizeof(s.openaiKey), activeKey);
            else if (prov == "openai" && hasBase && !baseLoopback && !s.cloudKey[0]) copyStr(s.cloudKey, sizeof(s.cloudKey), activeKey);
        }
        // Seed the shipped Gemini key on first run so the copilot works out of the box.
        if (!s.geminiKey[0]) copyStr(s.geminiKey, sizeof(s.geminiKey), kSeededGeminiKey);
        copyStr(s.reasoningEffort, sizeof(s.reasoningEffort), j.value("reasoning_effort", std::string("high")));
        if (s.provider == 3) {
            std::string lbase = llama.value("base_url", "http://127.0.0.1:1234/v1");
            std::strncpy(s.baseUrl, lbase.c_str(), sizeof(s.baseUrl) - 1);
            s.vision = llama.value("vision", false);
        } else if (hasBase) {
            std::strncpy(s.baseUrl, base.c_str(), sizeof(s.baseUrl) - 1);
            s.vision = oai.value("vision", false);
        }
        s.offlineMode = j.value("offline_mode", false);
        s.planMode = j.value("plan_mode", true);
        s.maxIters = j.value("max_iters", 0);
        s.maxTokens = j.value("max_output_tokens", 0);
        s.contextWindow = j.value("context_window", 0);
        s.systemPrompt = j.value("system_prompt", "");
        s.theme = j.value("theme", "dark");
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
        }
        // T-G21: pinned-column vertical-collapse state.
        s.fmCollapsed = j.value("fm_collapsed", false);
        s.copilotCollapsed = j.value("copilot_collapsed", false);
        // Wave 3: File Explorer workspace + Recent files.
        s.workspaceDir = j.value("workspace_dir", std::string());
        s.recentFiles.clear();
        if (auto it = j.find("recent_files"); it != j.end() && it->is_array())
            for (const auto& e : *it)
                if (e.is_string()) s.recentFiles.push_back(e.get<std::string>());
        // Chat-picker model visibility (opt-out hidden set).
        s.hiddenModels.clear();
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
}

void saveSettings(const Settings& s) {
    nlohmann::json j;
    if (s.provider == 4) {
        j["provider"] = "gemini";
        j["model"] = s.model[0] ? s.model : "gemini-2.5-flash";
        j["api_key"] = s.geminiKey;
    } else if (s.provider == 0) {
        j["provider"] = "anthropic";
        j["model"] = s.model[0] ? s.model : "claude-opus-4-8";
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
        // Cloud OpenAI-compatible API (DashScope / DeepSeek / ...). Same wire shape as a local
        // openai endpoint but with a CLOUD base_url, so resolve_provider's offline guard refuses it
        // under ITAR mode while local stays allowed.
        j["provider"] = "openai";
        j["model"] = s.model[0] ? s.model : "qwen-max";
        j["api_key"] = s.cloudKey;
        j["openai"] = {{"base_url", s.baseUrl[0] ? s.baseUrl
                                                 : "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
                       {"vision", s.vision}};
    } else {
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
    // can tell which providers are usable. The active provider's key is also the top-level
    // api_key above (what resolve_provider reads).
    j["keys"] = {{"gemini", s.geminiKey}, {"anthropic", s.anthropicKey}, {"openai", s.openaiKey},
                 {"cloud_compat", s.cloudKey}};
    // Reasoning effort applies only to reasoning-capable models; resolve_provider passes it
    // through to the provider (gemini thinkingConfig / anthropic effort).
    if (s.reasoningEffort[0]) j["reasoning_effort"] = s.reasoningEffort;
    j["offline_mode"] = s.offlineMode;
    j["plan_mode"] = s.planMode;
    if (s.maxIters > 0) j["max_iters"] = s.maxIters;
    if (s.maxTokens > 0) j["max_output_tokens"] = s.maxTokens;
    if (s.contextWindow > 0) j["context_window"] = s.contextWindow;
    if (!s.systemPrompt.empty()) j["system_prompt"] = s.systemPrompt;
    j["theme"] = s.theme;
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
        if (!glassJ.empty()) j["appearance"] = {{"glass", glassJ}};
    }
    // T-G21: pinned-column vertical-collapse state.
    j["fm_collapsed"] = s.fmCollapsed;
    j["copilot_collapsed"] = s.copilotCollapsed;
    // Wave 3: File Explorer workspace + Recent files.
    if (!s.workspaceDir.empty()) j["workspace_dir"] = s.workspaceDir;
    j["recent_files"] = s.recentFiles;
    if (!s.hiddenModels.empty())
        j["hidden_models"] = std::vector<std::string>(s.hiddenModels.begin(), s.hiddenModels.end());
    if (!s.disabledProviders.empty())
        j["disabled_providers"] =
            std::vector<std::string>(s.disabledProviders.begin(), s.disabledProviders.end());
    // Experimental AI features. Only emit the block when something is enabled/customized, so a
    // default config stays clean and the core's apply_experimental is a no-op.
    if (s.expVisionReview || s.expCadSequenceMode) {
        nlohmann::json exp = nlohmann::json::object();
        if (s.expVisionReview) {
            exp["vision_review"] = {
                {"enabled", true},
                {"model", s.expVisionModel[0] ? s.expVisionModel : "qwen2.5-vl-7b-instruct"},
                {"base_url", s.expVisionBaseUrl[0] ? s.expVisionBaseUrl
                                                   : "http://localhost:1234/v1"},
                {"api_key", s.expVisionKey},
            };
        }
        if (s.expCadSequenceMode) exp["cad_sequence_mode"] = true;
        j["experimental"] = exp;
    }
    try {
        std::filesystem::create_directories(appdataAppDir());
        std::ofstream(configPath()) << j.dump(2);
    } catch (...) {
    }
}

// ---------------- textures ----------------
Tex loadTexture(const std::string& path) {
    Tex t;
    int n;
    unsigned char* d = stbi_load(path.c_str(), &t.w, &t.h, &n, 4);
    if (!d) return t;
    glGenTextures(1, &t.id);
    glBindTexture(GL_TEXTURE_2D, t.id);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, t.w, t.h, 0, GL_RGBA, GL_UNSIGNED_BYTE, d);
    stbi_image_free(d);
    return t;
}
void setWindowIcon(GLFWwindow* win, const std::string& path) {
    int w, h, n;
    unsigned char* src = stbi_load(path.c_str(), &w, &h, &n, 4);
    if (!src) return;

    static const int kSizes[] = {16, 32, 48, 128, 256};
    static const int kCount   = (int)(sizeof(kSizes) / sizeof(kSizes[0]));

    GLFWimage imgs[kCount];
    unsigned char* bufs[kCount] = {};
    int nImgs = 0;

    for (int i = 0; i < kCount; ++i) {
        int s = kSizes[i];
        if (s > w || s > h) continue;   // don't upscale
        unsigned char* buf = (unsigned char*)malloc(s * s * 4);
        if (!buf) continue;
        stbir_resize_uint8_srgb(src, w, h, 0, buf, s, s, 0, STBIR_RGBA);
        imgs[nImgs]  = {s, s, buf};
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
    r.ok = f.value("ok", true);
    r.summary = jstr(f, "summary");
    auto pairs = [](const nlohmann::json& arr, const char* valKey) {
        std::vector<FitPair> v;
        if (arr.is_array())
            for (auto& e : arr) {
                if (!e.is_object()) continue;
                FitPair fp;
                fp.a = jstr(e, "a");
                fp.b = jstr(e, "b");
                fp.value = jnum(e, valKey);
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

void freeChatTextures(std::vector<ChatMsg>& chat) {
    for (auto& m : chat)
        for (auto& s : m.steps)
            if (s.thumb.id) { glDeleteTextures(1, &s.thumb.id); s.thumb.id = 0; }
}

// ---------------- AppState methods (formerly run_gui lambdas) ----------------
void AppState::addLog(const std::string& s) {
    log.push_back(s);
    if (log.size() > 500) log.erase(log.begin());
    // C40: tee to a file when AI_CAD_LOG_FILE is set — the in-app console is hard to reach
    // when diagnosing interaction bugs (and stdout is lost for a windowed exe).
    static FILE* tee = [] {
        const char* p = std::getenv("AI_CAD_LOG_FILE");
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

// W1-B: post a transient toast. born is filled by drawToasts on the first frame it's seen (we can't
// reach ImGui's clock from here cheaply), so we leave born=0 and let drawToasts stamp it.
// C39: thread-safe — the worker errorSink calls this from background threads.
void AppState::toast(ui::ToastLevel level, std::string msg) {
    if (suppressToasts) return;
    ui::Toast t;
    t.level = level;
    t.msg = std::move(msg);
    t.born = 0.0;  // 0 = unstamped; drawToasts stamps it with ImGui::GetTime() on first sight
    t.ttl = ui::toastTtl(level);
    std::lock_guard<std::mutex> lk(toastsMtx);
    toasts.push_back(std::move(t));
    // Cap the backing queue so an error storm can't grow it without bound (drawToasts shows <= 5).
    if (toasts.size() > 32) toasts.erase(toasts.begin(), toasts.begin() + (toasts.size() - 32));
}

// W1-B (item 4): shared undo/redo. Both the tree toolbar and the global keys call these.
core::OpsResponse AppState::docUndo() {
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
    workerStarted = worker.start(werr);
    if (workerStarted) {
        status = "Ready.";
        statusKind = 0;
        addLog("[ok] kernel worker ready");
        return;
    }
    addLog("[err] worker: " + werr);
#ifndef _WIN32
    // No managed Python runtime on this platform — the in-process C++ core (libaicad) is the
    // backend (doc/agent/sim ops route through cpp_core). If it loads, the app is ready; do NOT
    // dead-end into Python provisioning. On Windows the Python worker stays the production path.
    {
        std::string lerr;
        if (core::cpp_core().ensureLoaded(lerr)) {
            // Treat the in-process C++ core as the live backend: the ~50 `workerStarted` gates
            // across the UI (FeatureManager toolbar / doc ops / panels) must pass, since RPCs
            // route to libaicad via worker_client. (worker_client's own `running_` stays false,
            // so methods libaicad can't serve still fail cleanly rather than hit a dead pipe.)
            workerStarted = true;
            status = "Ready.";
            statusKind = 0;
            addLog("[ok] in-process C++ core (libaicad) ready; Python worker not required");
            return;
        }
        addLog("[..] libaicad not loaded: " + lerr);
    }
#endif
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
    // 4. In-sketch entity + relation selection (no-op when not in a sketch).
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
            job = std::move(g_meshQueue.front());
            g_meshQueue.pop_front();
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
                                    bool renderOnly);

bool AppState::loadDocMesh(const std::string& aicmPath, std::string& err, bool renderOnly) {
    if (!renderer || !idPass || !overlays) { err = "render objects not initialized"; return false; }
    render::DocMesh doc;
    if (!render::loadAicmFile(aicmPath, doc, err)) return false;
    return commitDocMeshFromParsed(*this, doc, err, renderOnly);
}

void AppState::loadDocMeshAsync(const std::string& aicmPath, bool renderOnly) {
    if (aicmPath.empty()) return;
    if (!renderer || !idPass || !overlays) return;
    ensureMeshLoader();
    const uint64_t gen = ++meshGen_;
    {
        std::lock_guard<std::mutex> lk(g_meshMtx);
        // Drop queued (not yet parsing) jobs — latest-wins at the queue too.
        g_meshQueue.clear();
        g_meshQueue.push_back(MeshLoadJob{gen, aicmPath, renderOnly, false, {}, {}});
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
    // Apply only the newest ready job that still matches meshGen_ (drop everything older).
    MeshLoadJob* best = nullptr;
    for (auto& j : ready) {
        if (j.gen == cur) best = &j;
    }
    if (!best) return;  // all stale — keep last-good mesh on screen
    if (!best->ok) {
        addLog("[err] loadDocMeshAsync: " + best->err);
        return;
    }
    std::string err;
    if (!commitDocMeshFromParsed(*this, best->doc, err, best->renderOnly) && !err.empty())
        addLog("[err] loadDocMeshAsync commit: " + err);
}

static bool commitDocMeshFromParsed(AppState& st, render::DocMesh& doc, std::string& err,
                                    bool renderOnly) {
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
        if (st.renderer) st.renderer->setMeshes(std::vector<Renderer::PartMesh>{});
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
        return true;
    }

    // Shaded view: render each body with its AICM color (camera bounds come from here).
    // C19: attach any SCLR / DISP overlay arrays so the st.renderer can colormap + deform.
    std::vector<Renderer::PartMesh> pms;
    std::vector<uint32_t> newBodyIds;  // AICM body id per st.renderer slot (parallels pms)
    for (const auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        newBodyIds.push_back(b.body_id);
        Renderer::PartMesh pm;
        pm.mesh = meshFromBody(b);
        pm.color[0] = b.color[0]; pm.color[1] = b.color[1]; pm.color[2] = b.color[2];
        pm.metallic = b.metallic; pm.roughness = b.roughness;  // C32-A RealView material
        // Per-feature edit-preview isolation: carry the body id + per-face triangle runs so the
        // st.renderer can draw a single feature's faces translucent. meshFromBody copies b.idx
        // verbatim, so each FaceRange's first_tri/tri_count still index this body's EBO 1:1.
        pm.body_id = b.body_id;
        pm.faces.reserve(b.faces.size());
        for (const auto& fr : b.faces)
            pm.faces.push_back({fr.face_id, fr.first_tri, fr.tri_count});
        // Scalar field for this body. Prefer the user-selected result field by name
        // (st.activeScalarField — von Mises / principal / shear / disp component / FoS …) so switching
        // result fields is a cheap renderOnly re-load of the same overlay with no re-solve; fall back
        // to the first scalar present when the named field isn't in this overlay.
        const render::ScalarField* chosen = nullptr;
        for (const auto& sc : doc.scalars) {
            if (sc.body_id != b.body_id || sc.values.size() != b.n_verts) continue;
            if (sc.name == st.activeScalarField) { chosen = &sc; break; }
            if (!chosen) chosen = &sc;
        }
        if (chosen) {
            pm.scalar = chosen->values;
            pm.scalarMin = chosen->vmin;
            pm.scalarMax = chosen->vmax;
        }
        // First displacement field for this body (FEA deformed shape).
        for (const auto& df : doc.displacements) {
            if (df.body_id == b.body_id && df.vec.size() == (size_t)b.n_verts * 3) {
                pm.disp = df.vec;
                pm.dispScaleHint = df.scale_hint;
                break;
            }
        }
        pms.push_back(std::move(pm));
    }
    // C48: unconditional — pms can be empty when every body has no triangles; keeping the
    // previous meshes would desync the viewport from the document (same stale-mesh bug as
    // the zero-bodies case above).
    st.renderer->setMeshes(pms);

    // C19: surface overlay presence for the simulation panel's result controls.
    st.docMeshHasScalar = false;
    st.docMeshHasDisp = false;
    for (const auto& pm : pms) {
        if (!pm.scalar.empty()) st.docMeshHasScalar = true;
        if (!pm.disp.empty()) { st.docMeshHasDisp = true; st.dispScaleHint = pm.dispScaleHint; }
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
    // Doc-driven visibility (runs AFTER docBodyIds/bodyVisible + hasDocMesh are set above): apply
    // persisted assembly-component `hidden` flags to the body mask, and auto-hide user reference
    // planes that a 3D feature now references. Guarded internally against non-matching schemas.
    st.applyDocDrivenVisibility(st.docMirror.tree());
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
    if (renderer) renderer->setMeshes(std::vector<Renderer::PartMesh>{});
    clearPickBuffers(idPass, overlays);
    hasDocMesh = false;
    // Solid Bodies visibility: a fresh doc starts with no bodies and nothing hidden (stale hides
    // must never leak into the next document).
    bodyVisible.clear();
    docBodyIds.clear();
    hiddenFeatures.clear();
    autoHiddenPlanes.clear();
    // T-BL37: origin point glyph is hidden by default (matches planes' hidden-by-default
    // behavior). The FM eye toggle removes this entry to show it.
    hiddenFeatures.insert("point.origin");
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

// Reserved reference datums (Front/Top/Right/origin/axes) are the app-owned overlays whose
// auto-show/hide lives in viewport_panel (showPlane[]/originUserToggled). Only USER datum planes
// (feature type "plane") are auto-hidden by this helper, so we never fight that path.
static bool isReservedDatumFid(const std::string& s) {
    return s == "origin" || s == "point.origin" ||
           s == "plane.front" || s == "plane.top" || s == "plane.right" ||
           s == "axis.x" || s == "axis.y" || s == "axis.z";
}

// Pull a datum feature id out of a selector value. Accepts {"kind":"datum","feature":"<fid>"}
// (canonical) and the short {"datum":"<fid>"} form. Returns "" for face/edge/vertex selectors.
static std::string datumFidOf(const nlohmann::json& sel) {
    if (!sel.is_object()) return {};
    if (sel.value("kind", std::string()) == "datum") {
        auto it = sel.find("feature");
        if (it != sel.end() && it->is_string()) return it->get<std::string>();
    }
    auto it = sel.find("datum");
    if (it != sel.end() && it->is_string()) return it->get<std::string>();
    return {};
}

void AppState::applyDocDrivenVisibility(const nlohmann::json& tree) {
    if (!tree.is_object()) return;
    const bool isAsm = tree.value("schema", std::string()) == "aicad.asm/1";

    if (isAsm) {
        // Assembly: re-derive the per-component hide mask from document.json. The engine builds one
        // body per NON-suppressed component, 1-based ordinal == body_id (assembly.cpp), so we walk
        // components in tree order, skip suppressed (they produce no body), and for each `hidden`
        // one drop the renderer slot whose docBodyId equals that ordinal. Suppressed components
        // shift ordinals, so replicating the enumeration here is what keeps the mapping correct.
        auto ci = tree.find("components");
        if (ci == tree.end() || !ci->is_array()) return;
        // Drive each component's body slot DIRECTLY from its `hidden` flag (two-way, idempotent).
        // A same-body-set reload preserves bodyVisible (commitDocMeshFromParsed only resets it when
        // the body-id list changes), and toggling `hidden` — unlike `suppressed` — leaves the body
        // set identical. So an un-hide must actively set the slot back to 1: a one-way `=0` would
        // never restore the body. In an assembly there's exactly one body per component, so keying
        // the slot straight off the flag can't clobber a separate per-body toggle (there is none).
        uint32_t ordinal = 0;
        for (const auto& c : *ci) {
            if (!c.is_object()) continue;
            if (c.value("suppressed", false)) continue;   // no body built -> no slot
            ++ordinal;                                      // matches assembly.cpp body_id
            uint8_t vis = c.value("hidden", false) ? 0 : 1;
            for (size_t slot = 0; slot < docBodyIds.size(); ++slot)
                if (docBodyIds[slot] == ordinal && slot < bodyVisible.size())
                    bodyVisible[slot] = vis;
        }
        applyBodyVisibility();
        return;
    }

    // Part: auto-hide USER datum planes once a solid feature references them (SolidWorks tidies a
    // reference plane out of view once you've sketched-and-built on it). We only act once there's
    // a body (hasDocMesh) — the same "you've committed a 3D feature" signal the origin/plane
    // auto-hide uses — and only ONCE per plane (recorded in autoHiddenPlanes), so a user who
    // re-shows the plane via the FM eye keeps it shown across later rebuilds.
    if (!hasDocMesh) return;
    auto fi = tree.find("features");
    if (fi == tree.end() || !fi->is_array()) return;

    // 1) user datum-plane fids present in the tree.
    std::unordered_set<std::string> userPlanes;
    for (const auto& f : *fi) {
        if (!f.is_object()) continue;
        if (f.value("type", std::string()) != "plane") continue;
        std::string fid = f.value("id", std::string());
        if (!fid.empty() && !isReservedDatumFid(fid)) userPlanes.insert(fid);
    }
    if (userPlanes.empty()) return;

    // 2) datum fids that a sketch sits on (sketch.refs.plane). A sketch is what a 3D feature
    //    (extrude/revolve/…) consumes, so "sketch on plane P + a body exists" == "P is referenced
    //    by a 3D feature" for the user's purposes. Also catch a 3D feature referencing a plane
    //    directly via any datum-typed selector in its refs.
    std::unordered_set<std::string> referenced;
    auto scanRefsForDatums = [&](const nlohmann::json& refs) {
        if (!refs.is_object()) return;
        for (auto it = refs.begin(); it != refs.end(); ++it) {
            const nlohmann::json& v = it.value();
            if (v.is_object()) { std::string d = datumFidOf(v); if (!d.empty()) referenced.insert(d); }
            else if (v.is_array())
                for (const auto& e : v) { std::string d = datumFidOf(e); if (!d.empty()) referenced.insert(d); }
        }
    };
    for (const auto& f : *fi) {
        if (!f.is_object()) continue;
        auto ri = f.find("refs");
        if (ri != f.end()) scanRefsForDatums(*ri);
    }

    bool changed = false;
    for (const std::string& p : userPlanes) {
        if (!referenced.count(p)) continue;
        if (autoHiddenPlanes.count(p)) continue;   // already auto-acted; respect any user re-show
        autoHiddenPlanes.insert(p);
        if (hiddenFeatures.insert(p).second) changed = true;
    }
    if (changed) applyDatumVisibility();
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
