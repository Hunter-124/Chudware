// host/src/ui/model_registry.h — the copilot model catalog + capability flags.
//
// Header-only so both settings_modal.cpp and legacy_chat.cpp can share it without a
// build-file change. Drives the "model picker": which models exist per provider, whether
// a model supports a reasoning-effort control, and whether it can see screenshots (vision).
// Live availability (key present / endpoint reachable / which local models are loaded) is
// resolved at runtime by AppState's model probe (agent_list_models RPC); this file is the
// static knowledge the probe augments. Probe rows may carry context / reasoning_efforts /
// vision / tools / auto_source — when present those win over the static heuristics below.
#pragma once

#include <cctype>
#include <string>
#include <vector>

namespace ui {

// Provider indices match Settings::provider (and the Settings AI-tab combo order).
enum ProviderIndex {
    PROV_ANTHROPIC = 0,
    PROV_OPENAI = 1,      // OpenAI-compatible CLOUD (api.openai.com) — distinct from local
    PROV_LOCAL = 2,       // Local OpenAI-compatible endpoint (LM Studio etc.)
    PROV_LLAMACPP = 3,    // Managed llama.cpp
    PROV_GEMINI = 4,      // Google Gemini (generativelanguage)
    PROV_CLOUDCOMPAT = 5, // Cloud OpenAI-compatible API w/ custom base_url (Alibaba DashScope /
                          // DeepSeek API / etc.). Resolves as provider "openai" + a cloud base_url,
                          // so the offline (ITAR) guard REFUSES it while local stays allowed.
    PROV_XAI = 6,         // xAI Grok — API key (console.x.ai) and/or SuperGrok OAuth (xai-sub)
    PROV_CURSOR = 7,      // Cursor (Composer + proxied Anthropic/OpenAI models)
    PROV_KIMISUB = 8,     // Kimi Code subscription — OAuth via subscription_auth (wire "kimi-sub")
    PROV_COUNT = 9,
};

struct ModelInfo {
    int provider = PROV_GEMINI;
    std::string id;        // wire model id (config.json "model")
    std::string label;     // human label for the picker
    bool vision = true;    // can receive screenshots
    bool reasoning = true; // exposes a reasoning-effort knob
    // ---- auto-discovered capability metadata (0 / empty = unknown → fall back) ----
    int context = 0;                     // total context window tokens
    int max_output = 0;                  // max output tokens (0 = unspecified)
    bool tools = true;                   // tool/function calling
    std::vector<std::string> reasoning_efforts;  // empty = use static ladder
    std::string auto_source;             // "live" | "models.dev" | "heuristic" | "user" | combo
    bool has_probe_meta = false;         // true when filled from agent_list_models model_rows
};


inline const char* providerLabel(int provider) {
    switch (provider) {
        case PROV_ANTHROPIC: return "Anthropic (Claude)";
        case PROV_OPENAI: return "OpenAI";
        case PROV_LOCAL: return "Local (OpenAI-compatible)";
        case PROV_LLAMACPP: return "llama.cpp (managed local)";
        case PROV_GEMINI: return "Google Gemini";
        case PROV_CLOUDCOMPAT: return "OpenRouter / Cloud API";
        case PROV_XAI: return "xAI (Grok)";
        case PROV_CURSOR: return "Cursor";
        case PROV_KIMISUB: return "Kimi Code (subscription)";
        default: return "?";
    }
}

inline bool providerIsCloud(int provider) {
    return provider == PROV_ANTHROPIC || provider == PROV_OPENAI || provider == PROV_GEMINI ||
           provider == PROV_CLOUDCOMPAT || provider == PROV_XAI || provider == PROV_CURSOR ||
           provider == PROV_KIMISUB;
}

// ---- provider/service CATEGORY keys (for Manage-models categories + bulk on/off) ----
// A stable string identity per provider category, used by Settings::disabledProviders and the
// collapsible Manage-models groups. Cloud-compat is sub-categorized by base URL (a single cloud
// slot serves OpenRouter / Alibaba / DeepSeek), so cloud rows use cloudServiceKey() instead.
inline const char* providerCatKey(int provider) {
    switch (provider) {
        case PROV_GEMINI: return "gemini";
        case PROV_ANTHROPIC: return "anthropic";
        case PROV_OPENAI: return "openai";
        case PROV_LOCAL:
        case PROV_LLAMACPP: return "local";
        case PROV_CLOUDCOMPAT: return "cloud";  // refine with cloudServiceKey(base_url)
        case PROV_XAI: return "xai";
        case PROV_CURSOR: return "cursor";
        case PROV_KIMISUB: return "kimi-code";
        default: return "other";
    }
}

// Map a cloud base URL to its service category key (OpenRouter / Alibaba / DeepSeek / generic).
inline const char* cloudServiceKey(const std::string& base_url) {
    if (base_url.find("openrouter.ai") != std::string::npos) return "openrouter";
    if (base_url.find("aliyuncs.com") != std::string::npos) return "alibaba";
    if (base_url.find("deepseek.com") != std::string::npos) return "deepseek";
    if (base_url.find("moonshot.ai") != std::string::npos) return "moonshot";
    return "cloud";
}

inline const char* categoryLabel(const std::string& key) {
    if (key == "gemini") return "Google Gemini";
    if (key == "anthropic") return "Anthropic (Claude)";
    if (key == "openai") return "OpenAI";
    if (key == "local") return "Local endpoint";
    if (key == "openrouter") return "OpenRouter";
    if (key == "alibaba") return "Alibaba Cloud (Qwen)";
    if (key == "deepseek") return "DeepSeek";
    if (key == "moonshot") return "Moonshot (Kimi)";
    if (key == "xai") return "xAI (Grok)";
    if (key == "cursor") return "Cursor";
    if (key == "kimi-code") return "Kimi Code (subscription)";
    return "Cloud API";
}

// The built-in catalog of KNOWN cloud models. Local/llama.cpp models are discovered live
// (a local endpoint can serve any GGUF), so they are NOT listed here — the probe fills them.
inline const std::vector<ModelInfo>& builtinModels() {
    static const std::vector<ModelInfo> kModels = {
        // Google Gemini — vision + thinking (reasoning-effort) on 2.5; 2.0-flash has no knob.
        {PROV_GEMINI, "gemini-2.5-pro", "Gemini 2.5 Pro", true, true},
        {PROV_GEMINI, "gemini-2.5-flash", "Gemini 2.5 Flash", true, true},
        {PROV_GEMINI, "gemini-2.0-flash", "Gemini 2.0 Flash", true, false},
        // Anthropic.
        {PROV_ANTHROPIC, "claude-opus-4-8", "Claude Opus 4.8", true, true},
        {PROV_ANTHROPIC, "claude-sonnet-5", "Claude Sonnet 5", true, true},
        {PROV_ANTHROPIC, "claude-fable-5", "Claude Fable 5", true, true},
        {PROV_ANTHROPIC, "claude-haiku-4-5", "Claude Haiku 4.5", true, false},
        // OpenAI cloud (gpt-5.6-sol is current; older ≤ gpt-5.5 and o3 purged).
        {PROV_OPENAI, "gpt-5.6-sol", "GPT-5.6 Sol", true, true},
        // Cursor — Composer + proxied Anthropic/OpenAI flagships.
        {PROV_CURSOR, "composer-2.5", "Cursor Composer 2.5", true, true},
        {PROV_CURSOR, "cursor/claude-opus-4-8", "Claude Opus 4.8 (Cursor)", true, true},
        {PROV_CURSOR, "cursor/claude-sonnet-5", "Claude Sonnet 5 (Cursor)", true, true},
        {PROV_CURSOR, "cursor/gpt-5.6-sol", "GPT-5.6 Sol (Cursor)", true, true},
        // Kimi Code subscription (wire provider "kimi-sub") — GUI ids kimi-k3 / kimi-k2.7.
        // K3 is the CAD-modeling priority model (1M ctx); K2.7 is the lighter secondary (262k).
        // Wire alias: kimi_sub_provider maps both → "kimi-for-coding" on the subscription endpoint.
        {PROV_KIMISUB, "kimi-k3", "Kimi K3 (1M)", true, true},
        {PROV_KIMISUB, "kimi-k2.7", "Kimi K2.7 (262K)", true, true},
        // xAI Grok — only grok-4.5 is user-visible (500k context; ~$2/$6 per Mtok).
        {PROV_XAI, "grok-4.5", "Grok 4.5 (default, 500k)", true, true},
    };
    return kModels;
}

// ---- experimental local-model presets (research-backed, ITAR-safe self-hostable) ----
// Local OpenAI-compatible servers (LM Studio / Ollama / vLLM) can serve ANY model, so these are
// NOT live-discovered like the cloud catalog — they are one-click presets that fill the model id
// (+ vision flag + a sensible default base URL) for the open-weight models the research flagged as
// strong fits for the copilot. The user still needs the weights loaded in their local server.
struct LocalPreset {
    std::string id;        // wire model id to request (must match the server's loaded model)
    std::string label;     // human label for the preset picker
    bool vision = false;   // can receive viewport screenshots (drives vision review/fix)
    std::string note;      // one-line "what it's good for" shown on hover
    std::string baseUrl;   // default endpoint (LM Studio 1234; user can override)
};

inline const std::vector<LocalPreset>& localPresets() {
    static const std::vector<LocalPreset> kPresets = {
        // Code / tool-calling backbones for structured CAD-op emission.
        {"qwen2.5-coder-7b-instruct", "Qwen2.5-Coder 7B (ops/code)", false,
         "Code model; reliable JSON + tool-calling for emitting CAD ops.",
         "http://localhost:1234/v1"},
        {"qwen2.5-coder-32b-instruct", "Qwen2.5-Coder 32B (ops/code)", false,
         "Larger coder; best structured-op quality if your GPU fits it.",
         "http://localhost:1234/v1"},
        {"qwen2.5-14b-instruct", "Qwen2.5 14B (general)", false,
         "General model; solid planner/intake without a vision head.",
         "http://localhost:1234/v1"},
        // Vision judges for the review/fix loop (beat Gemini 1.5 Pro on grounding in the research).
        {"qwen2.5-vl-7b-instruct", "Qwen2.5-VL 7B (vision review)", true,
         "Vision model; strong grounding on rendered viewport PNGs.",
         "http://localhost:1234/v1"},
        {"qwen2.5-vl-3b-instruct", "Qwen2.5-VL 3B (vision, light)", true,
         "Smaller vision judge for modest GPUs.",
         "http://localhost:1234/v1"},
        // Reasoning model behind the Seek-CAD-style local self-refinement loop.
        {"deepseek-r1-distill-qwen-32b", "DeepSeek-R1 Distill 32B (reasoning)", false,
         "Reasoning model; the Seek-CAD local design loop runs a Q4 of this on a single 24GB GPU.",
         "http://localhost:1234/v1"},
    };
    return kPresets;
}

// Cloud OpenAI-compatible API presets (PROV_CLOUDCOMPAT). Each bundles the service base URL with a
// model id, so picking one fills both. These are NETWORK providers: they are REFUSED when Offline
// (ITAR) mode is on — only the local open-weight presets above run under ITAR. DashScope = Alibaba
// Cloud's Qwen API; api.deepseek.com = the official DeepSeek API; api.moonshot.ai = Moonshot (Kimi).
inline const std::vector<LocalPreset>& cloudPresets() {
    static const std::string kDashScope = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1";
    static const std::string kDeepSeek = "https://api.deepseek.com/v1";
    static const std::string kOpenRouter = "https://openrouter.ai/api/v1";
    static const std::string kMoonshot = "https://api.moonshot.ai/v1";
    static const std::vector<LocalPreset> kPresets = {
        // OpenRouter — one key, many models (Qwen / DeepSeek / open-weight). ":free" tiers cost
        // nothing but are rate-limited upstream; paid slugs are cheap and reliable (needs credit /
        // a non-zero key spend limit). Listed first since it's the easiest working cloud path.
        // Qwen = current Qwen3 generation only (chat / vision / coder; no image/video generation).
        {"qwen/qwen3-coder:free", "OpenRouter · Qwen3-Coder (free)", false,
         "OpenRouter free tier — Qwen3 coder for op emission. Rate-limited; retries help.", kOpenRouter},
        {"qwen/qwen3-coder", "OpenRouter · Qwen3-Coder (paid)", false,
         "OpenRouter paid — reliable Qwen3 coder for structured ops (needs credit).", kOpenRouter},
        {"qwen/qwen3-vl-32b-instruct", "OpenRouter · Qwen3-VL 32B (vision, paid)", true,
         "OpenRouter paid — Qwen3 vision model, great as the review judge (needs credit).", kOpenRouter},
        {"qwen/qwen3-max", "OpenRouter · Qwen3-Max (flagship, paid)", false,
         "OpenRouter paid — Qwen3 flagship general model (needs credit).", kOpenRouter},
        {"deepseek/deepseek-chat", "OpenRouter · DeepSeek V3 (paid)", false,
         "OpenRouter paid — DeepSeek V3 general/coding (needs credit).", kOpenRouter},
        {"openai/gpt-oss-120b:free", "OpenRouter · GPT-OSS 120B (free)", false,
         "OpenRouter free tier — open-weight 120B, currently reliable.", kOpenRouter},
        // Alibaba Cloud (DashScope) — current Qwen3 generation (chat / vision / coder). Image/video
        // generation (qwen-image-*, wan*, z-image) and audio (qwen3-tts/asr/omni) are intentionally
        // excluded — they aren't text copilots.
        {"qwen3-max", "Alibaba Qwen3-Max (flagship)", false,
         "Alibaba Cloud DashScope. Current Qwen3 flagship; needs a DashScope API key.", kDashScope},
        {"qwen3-vl-plus", "Alibaba Qwen3-VL-Plus (vision)", true,
         "Alibaba Cloud DashScope Qwen3 vision model — great as the review judge.", kDashScope},
        {"qwen3-coder-plus", "Alibaba Qwen3-Coder-Plus (ops/code)", false,
         "Alibaba Cloud DashScope Qwen3 code model for structured op emission.", kDashScope},
        // DeepSeek official API.
        {"deepseek-chat", "DeepSeek V3 (chat)", false,
         "Official DeepSeek API. Strong general/coding model.", kDeepSeek},
        {"deepseek-reasoner", "DeepSeek R1 (reasoner)", false,
         "Official DeepSeek API reasoning model (R1).", kDeepSeek},
        // Moonshot (Kimi) — first-class "moonshot" wire provider (keys.moonshot), DIRECT
        // api.moonshot.ai billing. Label says "Moonshot API" to distinguish these rows from
        // the Kimi Code subscription (PROV_KIMISUB) and from OpenRouter's live catalog.
        // K3 is the CAD-modeling priority model; K2.7 is secondary.
        {"kimi-k3", "Moonshot API \xc2\xb7 Kimi K3 (1M, $3/$15)", true,
         "DIRECT api.moonshot.ai — bills keys.moonshot, NOT OpenRouter. 1M context, native vision, always-reasons. Preferred for CAD modeling.", kMoonshot},
        {"kimi-k2.7", "Moonshot API \xc2\xb7 Kimi K2.7 (262K)", true,
         "DIRECT api.moonshot.ai — bills keys.moonshot. Kimi K2.7 for lighter/leaf assignments; 262K context, vision.", kMoonshot},
    };
    return kPresets;
}

// Does (provider, model id) expose a reasoning-effort knob? Falls back to the provider
// default for unknown ids (local models route through gemma-style reasoning -> true).
// When *probe* carries has_probe_meta + reasoning_efforts, that is authoritative.
inline bool modelSupportsReasoning(int provider, const std::string& id,
                                   const ModelInfo* probe = nullptr) {
    if (probe && probe->has_probe_meta) {
        // Empty efforts with probe meta = model does not expose a dial.
        return !probe->reasoning_efforts.empty();
    }
    for (const auto& m : builtinModels())
        if (m.provider == provider && m.id == id) return m.reasoning;
    if (provider == PROV_LOCAL || provider == PROV_LLAMACPP) return true;  // gemma reasons
    return false;
}

// Per-model default context window when settings.contextWindow == 0 (auto).
// Prefer probe metadata (agent_list_models model_rows) when present.
inline int defaultContextWindowForModel(int provider, const std::string& id,
                                        const ModelInfo* probe = nullptr) {
    if (probe && probe->has_probe_meta && probe->context > 0)
        return probe->context;
    std::string m = id;
    for (char& c : m) c = (char)std::tolower((unsigned char)c);
    auto has = [&](const char* sub) -> bool {
        if (sub == nullptr || !*sub) return false;
        std::string s = sub;
        for (char& c : s) c = (char)std::tolower((unsigned char)c);
        return m.find(s) != std::string::npos;
    };
    if (has("1.5-pro")) return 2097152;
    if (has("1.5-flash") || has("2.0") || has("2.5") || has("flash") || has("gemini"))
        return 1048576;
    if (has("claude") || has("opus") || has("sonnet")) return 200000;
    if (has("gpt-4.1")) return 1047576;
    if (has("o1") || has("o3") || has("o4") || has("reasoner") || has("r1")) return 200000;
    if (has("gpt-4o") || has("gpt-4-turbo") || has("4o-mini")) return 128000;
    if (has("gpt-3.5")) return 16385;
    if (has("qwen") || has("deepseek")) return 128000;
    // Moonshot Kimi / Kimi Code — check k2.7 / 256k / for-coding BEFORE bare kimi-k3
    // (kimi-k3-256k contains "kimi-k3"). K3 = 1M; K2.7 = 262k.
    if (has("kimi-k3-256k") || has("kimi-k2.7") || has("kimi-k2") || has("kimi-for-coding"))
        return 262144;
    if (has("kimi-k3")) return 1048576;
    // Grok 4.5 ships with a 500k context window; older grok-4.x is ~128k.
    if (has("grok-4.5") || has("grok-4-5")) return 500000;
    if (has("grok")) return 131072;
    switch (provider) {
        case PROV_GEMINI: return 1048576;
        case PROV_ANTHROPIC: return 200000;
        case PROV_OPENAI:
        case PROV_CLOUDCOMPAT:
        case PROV_XAI:
        case PROV_CURSOR: return 128000;
        case PROV_KIMISUB: return 1048576;  // default K3 = 1M
        default: return 8192;
    }
}

// Reasoning-effort levels for the active model. Returns count; writes labels/values arrays.
// When *probe* has reasoning_efforts, those values win (labels title-cased from values).
inline int reasoningEffortOptions(int provider, const std::string& id,
                                  const char** outLabels, const char** outValues, int maxN,
                                  const ModelInfo* probe = nullptr) {
    // Static storage for probe-derived C strings (valid until next call).
    static thread_local std::vector<std::string> s_vals;
    static thread_local std::vector<std::string> s_labs;
    static thread_local std::vector<const char*> s_valp;
    static thread_local std::vector<const char*> s_labp;

    if (probe && probe->has_probe_meta) {
        if (probe->reasoning_efforts.empty()) return 0;
        s_vals = probe->reasoning_efforts;
        s_labs.clear();
        s_labs.reserve(s_vals.size());
        for (const auto& v : s_vals) {
            std::string lab = v;
            if (!lab.empty()) lab[0] = (char)std::toupper((unsigned char)lab[0]);
            s_labs.push_back(lab);
        }
        s_valp.resize(s_vals.size());
        s_labp.resize(s_labs.size());
        int out = (int)s_vals.size() < maxN ? (int)s_vals.size() : maxN;
        for (int i = 0; i < out; ++i) {
            s_valp[i] = s_vals[i].c_str();
            s_labp[i] = s_labs[i].c_str();
            outLabels[i] = s_labp[i];
            outValues[i] = s_valp[i];
        }
        return out;
    }

    if (!modelSupportsReasoning(provider, id, probe)) return 0;
    struct Opt { const char* label; const char* value; };
    static const Opt kGemini[] = {{"Off", "off"}, {"Low", "low"}, {"Medium", "medium"},
                                  {"High", "high"}, {"Max", "max"}};
    static const Opt kAnthropic[] = {{"Low", "low"}, {"Medium", "medium"}, {"High", "high"}};
    static const Opt kGeneric[] = {{"Off", "off"}, {"Low", "low"}, {"Medium", "medium"},
                                   {"High", "high"}};
    std::string m = id;
    for (char& c : m) c = (char)std::tolower((unsigned char)c);
    const Opt* src = kGeneric;
    int n = 4;
    if (provider == PROV_GEMINI || m.find("gemini") != std::string::npos) {
        src = kGemini; n = 5;
    } else if (provider == PROV_ANTHROPIC || m.find("claude") != std::string::npos) {
        src = kAnthropic; n = 3;
    }
    int out = n < maxN ? n : maxN;
    for (int i = 0; i < out; ++i) {
        outLabels[i] = src[i].label;
        outValues[i] = src[i].value;
    }
    return out;
}

// Build a ModelInfo from an agent_list_models model_rows entry (JSON-ish fields already
// extracted by the caller). Marks has_probe_meta so the helpers above prefer it.
inline ModelInfo modelInfoFromProbe(int provider, const std::string& id,
                                    const std::string& label, int context, int max_output,
                                    bool vision, bool tools,
                                    const std::vector<std::string>& efforts,
                                    const std::string& auto_source) {
    ModelInfo m;
    m.provider = provider;
    m.id = id;
    m.label = label.empty() ? id : label;
    m.vision = vision;
    m.reasoning = !efforts.empty();
    m.context = context;
    m.max_output = max_output;
    m.tools = tools;
    m.reasoning_efforts = efforts;
    m.auto_source = auto_source;
    m.has_probe_meta = true;
    return m;
}


}  // namespace ui
