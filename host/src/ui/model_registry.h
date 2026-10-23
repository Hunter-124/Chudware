// host/src/ui/model_registry.h — the copilot model catalog + capability flags.
//
// Header-only so both settings_modal.cpp and copilot/settings.cpp can share it without a
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
    PROV_KIMISUB = 8,     // Kimi Code — online auth/execution through Chudcode.
    PROV_ZENMUX = 9,      // ZenMux — online auth/execution through Chudcode. Default provider
                          // (design decision 2026-07-23: free x-ai/grok-4.5-free tier works
                          // without an xAI sign-in).
    PROV_FREELLMAPI = 10, // Free AI (FreeLLMAPI, github.com/tashfeenahmed/freellmapi) — a
                          // self-hosted OpenAI-compatible router stacking ~28 providers' free
                          // tiers behind one endpoint + one unified key. Own native wire
                          // "freellmapi" (core/agent/providers.cpp + config.cpp) — NOT the
                          // shared "openai" slot, so it stays correctly cloud-classified
                          // (offline/ITAR mode refuses it) even though its default endpoint is
                          // http://localhost:3001/v1 (the router itself runs locally but
                          // forwards to real cloud model providers over the internet).
    PROV_COUNT = 11,
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
        case PROV_ZENMUX: return "ZenMux";
        case PROV_FREELLMAPI: return "Free AI (FreeLLMAPI)";
        default: return "?";
    }
}

inline bool providerIsCloud(int provider) {
    return provider == PROV_ANTHROPIC || provider == PROV_OPENAI || provider == PROV_GEMINI ||
           provider == PROV_CLOUDCOMPAT || provider == PROV_XAI || provider == PROV_CURSOR ||
           provider == PROV_KIMISUB || provider == PROV_ZENMUX || provider == PROV_FREELLMAPI;
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
        case PROV_ZENMUX: return "zenmux";
        case PROV_FREELLMAPI: return "freellmapi";
        default: return "other";
    }
}

// True when a base URL points at this machine (LM Studio / Ollama / vLLM / managed llama.cpp).
// Loopback endpoints need no credential; every other endpoint is a cloud service whose key must
// come from the matching keys.* slot. Substring match, not a URL parse: base URLs here are
// user-typed and may lack a scheme, and a host that merely CONTAINS "localhost" is still local.
inline bool baseUrlIsLoopback(const std::string& base_url) {
    return base_url.find("localhost") != std::string::npos ||
           base_url.find("127.0.0.1") != std::string::npos ||
           base_url.find("0.0.0.0") != std::string::npos ||
           base_url.find("[::1]") != std::string::npos;
}

// Map a cloud base URL to its service category key. Hosted services without a native
// OpenAI-compatible transport use the URL only as a service discriminator; Chudcode owns
// their real endpoint and credential. The same key drives picker badges, logos, and
// cloudServiceWireName() below.
inline const char* cloudServiceKey(const std::string& base_url) {
    if (base_url.find("openrouter.ai") != std::string::npos) return "openrouter";
    if (base_url.find("aliyuncs.com") != std::string::npos) return "alibaba";
    if (base_url.find("deepseek.com") != std::string::npos) return "deepseek";
    if (base_url.find("moonshot.ai") != std::string::npos) return "moonshot";
    if (base_url.find("amazonaws.com") != std::string::npos) return "amazon-bedrock";
    if (base_url.find("azure.com") != std::string::npos) return "azure";
    if (base_url.find("antigravity.google") != std::string::npos) return "google-antigravity";
    if (base_url.find("aimlapi.com") != std::string::npos) return "aimlapi";
    if (base_url.find("chatgpt.com") != std::string::npos) return "openai-codex";
    return "cloud";
}

// Cloud-slot service key -> the wire provider name Chudcode expects. "alibaba" is the picker
// category and maps to "alibaba-coding-plan"; "moonshot" keeps its native diagnostic identity,
// while an unclassified endpoint stays generic OpenAI-compatible. Exhaustive over
// cloudServiceKey()'s range; literals only, so the result never dangles.
inline const char* cloudServiceWireName(const std::string& key) {
    if (key == "openrouter") return "openrouter";
    if (key == "alibaba") return "alibaba-coding-plan";
    if (key == "deepseek") return "deepseek";
    if (key == "moonshot") return "moonshot";
    if (key == "amazon-bedrock") return "amazon-bedrock";
    if (key == "azure") return "azure";
    if (key == "google-antigravity") return "google-antigravity";
    if (key == "aimlapi") return "aimlapi";
    if (key == "openai-codex") return "openai-codex";
    return "openai";
}

// True when the cloud slot's own api-key field holds this service's credential. False for
// services authenticated through Chudcode, where forwarding an unrelated key from the shared
// slot would override the intended account.
inline bool cloudServiceUsesLocalKey(const std::string& key) {
    return key != "amazon-bedrock" && key != "azure" && key != "google-antigravity" &&
           key != "aimlapi" && key != "openai-codex";
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
    if (key == "amazon-bedrock") return "Amazon Bedrock";
    if (key == "azure") return "Azure OpenAI";
    if (key == "google-antigravity") return "Google Antigravity";
    if (key == "aimlapi") return "AI/ML API";
    if (key == "openai-codex") return "OpenAI Codex (subscription)";
    if (key == "xai") return "xAI (Grok)";
    if (key == "cursor") return "Cursor";
    if (key == "kimi-code") return "Kimi Code (subscription)";
    if (key == "zenmux") return "ZenMux";
    if (key == "freellmapi") return "Free AI (FreeLLMAPI)";
    return "Cloud API";
}

// providerLabel() for a slot, refined by its endpoint. PROV_CLOUDCOMPAT is ONE slot shared by
// every base_url-keyed service, so its generic "OpenRouter / Cloud API" label reads as a lie
// whenever the active endpoint is Moonshot / DeepSeek / DashScope — name the real service
// instead. Every other slot has a fixed identity and falls through to providerLabel().
inline const char* providerLabelForEndpoint(int provider, const char* base_url) {
    if (provider == PROV_CLOUDCOMPAT && base_url && base_url[0])
        return categoryLabel(cloudServiceKey(base_url));
    return providerLabel(provider);
}

// The built-in catalog of KNOWN cloud models. Local/llama.cpp models are discovered live
// (a local endpoint can serve any GGUF), so they are NOT listed here — the probe fills them.
inline const std::vector<ModelInfo>& builtinModels() {
    static const std::vector<ModelInfo> kModels = {
        // ZenMux; default since 2026-07-23. Free x-ai/grok-4.5-free tier needs only a
        // ZenMux sign-in through Chudcode, not an xAI credential.
        {PROV_ZENMUX, "x-ai/grok-4.5-free", "Grok 4.5 (default, free via ZenMux)", true, true},
        {PROV_ZENMUX, "moonshotai/kimi-k3", "Kimi K3 (via ZenMux)", true, true},
        {PROV_ZENMUX, "moonshotai/kimi-k3-free", "Kimi K3 (free via ZenMux)", true, true},
        // Google API wire ids (the catalog overrides these offline fallback rows).
        {PROV_GEMINI, "gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview", true, true},
        {PROV_GEMINI, "gemini-3.5-flash", "Gemini 3.5 Flash", true, true},
        // Anthropic offline fallback; the live Chudcode catalog is authoritative online.
        {PROV_ANTHROPIC, "claude-opus-5-5", "Claude Opus 5.5", true, true},
        {PROV_ANTHROPIC, "claude-sonnet-5-5", "Claude Sonnet 5.5", true, true},
        {PROV_ANTHROPIC, "claude-opus-5", "Claude Opus 5", true, true},
        {PROV_ANTHROPIC, "claude-sonnet-5", "Claude Sonnet 5", true, true},
        {PROV_ANTHROPIC, "claude-fable-5", "Claude Fable 5", true, true},
        {PROV_ANTHROPIC, "claude-mythos-5", "Claude Mythos 5", true, true},
        {PROV_ANTHROPIC, "claude-haiku-4-5", "Claude Haiku 4.5", true, true},
        // OpenAI cloud (gpt-6-sol is current, 2026-09-22; older ≤ gpt-5.5 and o3 purged).
        {PROV_OPENAI, "gpt-6-sol", "GPT-6 Sol", true, true},
        {PROV_OPENAI, "gpt-6-luna", "GPT-6 Luna", true, true},
        // Cursor — Composer only. The proxied cursor/claude-* + cursor/gpt-* rows were
        // removed 2026-07-21: Cursor rejects those wire ids upstream
        // (ERROR_BAD_MODEL_NAME), and composer-1/1.5 are sunset — every pick failed.
        {PROV_CURSOR, "composer-2.5", "Cursor Composer 2.5", true, true},
        {PROV_CURSOR, "composer-2", "Cursor Composer 2", true, true},
        // Kimi Code wire ids. K3 is the CAD-modeling priority model (1M context); K3-256k
        // caps the window for cheaper turns; K2.7 Coding is the lighter secondary.
        {PROV_KIMISUB, "k3", "Kimi K3 (1M)", true, true},
        {PROV_KIMISUB, "k3-256k", "Kimi K3-256k (256K)", true, true},
        {PROV_KIMISUB, "kimi-for-coding", "Kimi K2.7 Coding (262K)", true, true},
        // xAI Grok — only grok-4.5 is user-visible (500k context; ~$2/$6 per Mtok). No longer the
        // default (needs an explicit xAI sign-in) — kept for users who ARE signed into xAI.
        {PROV_XAI, "grok-4.5", "Grok 4.5 (500k)", true, true},
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

// Free AI (FreeLLMAPI, PROV_FREELLMAPI) quick-fill presets — the router's own virtual "meta"
// models (see docs/api.md#auto-routing-strategies upstream): "auto" picks the best available
// free model per request with automatic failover, "auto:fast"/"auto:smart" bias the same router
// toward speed or capability, and "fusion" fans a prompt out to a panel of free models in
// parallel and synthesizes one answer. The 339 concrete provider/model ids behind these are
// dynamic (freellmapi.co/models) and change weekly, so unlike builtinModels() this is NOT a
// live-discovered catalog — the model field stays free-text and these are just starting points.
inline const std::vector<LocalPreset>& freellmapiPresets() {
    static const std::vector<LocalPreset> kPresets = {
        {"auto", "Auto (best available, recommended)", true,
         "Router picks the best free model per request; falls over to the next on 429/5xx.", ""},
        {"auto:fast", "Auto — fast", false,
         "Biases routing toward the quickest free model (small/quantized).", ""},
        {"auto:smart", "Auto — smart", true,
         "Biases routing toward the most capable free model available right now.", ""},
        {"fusion", "Fusion (multi-model synthesis)", true,
         "Fans the prompt to several free models in parallel; a judge model synthesizes one "
         "answer. Slower and burns more free-tier budget per request.", ""},
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
    // Unknown revisions inherit a family ladder until catalog metadata arrives.
    // The catalog's explicit empty efforts list above always overrides this.
    std::string family = id;
    for (char& c : family) c = (char)std::tolower((unsigned char)c);
    if (family.find("claude-") != std::string::npos ||
        family.rfind("gpt-6", 0) == 0 || family.rfind("gpt-7", 0) == 0 ||
        family.rfind("gpt-5.6", 0) == 0 || family.rfind("gemini-3", 0) == 0 ||
        family.rfind("kimi-k3", 0) != std::string::npos)
        return true;
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
    if (has("opus-5") || has("sonnet-5")) return 1000000;
    if (has("claude") || has("opus") || has("sonnet")) return 200000;
    if (has("gpt-4.1")) return 1047576;
    if (has("o1") || has("o3") || has("o4") || has("reasoner") || has("r1")) return 200000;
    if (has("gpt-4o") || has("gpt-4-turbo") || has("4o-mini")) return 128000;
    if (has("gpt-3.5")) return 16385;
    if (has("qwen") || has("deepseek")) return 128000;
    // Moonshot Kimi / Kimi Code — check the 256k / k2.x / for-coding arms BEFORE the
    // bare-k3 one ("k3-256k" contains "k3"). K3 = 1M; K3-256k / K2.7 = 262k.
    if (has("k3-256k") || has("kimi-k2.7") || has("kimi-k2") || has("kimi-for-coding"))
        return 262144;
    if (has("kimi-k3") || has("k3")) return 1048576;
    // Grok 4.5 ships with a 500k context window; older grok-4.x is ~128k.
    if (has("grok-4.5") || has("grok-4-5")) return 500000;
    if (has("grok")) return 131072;
    switch (provider) {
        case PROV_GEMINI: return 1048576;
        case PROV_ANTHROPIC: return 200000;
        case PROV_OPENAI:
        case PROV_CLOUDCOMPAT:
        case PROV_XAI:
        case PROV_ZENMUX:
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
    static const Opt kGeminiLevel[] = {{"Minimal", "minimal"}, {"Low", "low"},
                                        {"Medium", "medium"}, {"High", "high"}};
    static const Opt kGeminiPro[] = {{"Low", "low"}, {"High", "high"}};
    static const Opt kClaudeAdaptive[] = {{"Low", "low"}, {"Medium", "medium"},
                                           {"High", "high"}, {"Xhigh", "xhigh"}, {"Max", "max"}};
    static const Opt kClaudeBudget[] = {{"Minimal", "minimal"}, {"Low", "low"},
                                         {"Medium", "medium"}, {"High", "high"}, {"Xhigh", "xhigh"}};
    static const Opt kClaude46[] = {{"Low", "low"}, {"Medium", "medium"},
                                     {"High", "high"}, {"Max", "max"}};
    static const Opt kClaudeSonnet46[] = {{"Low", "low"}, {"Medium", "medium"}, {"High", "high"}};
    static const Opt kKimiK3[] = {{"Low", "low"}, {"High", "high"}, {"Max", "max"}};
    static const Opt kGptModern[] = {{"Low", "low"}, {"Medium", "medium"},
                                      {"High", "high"}, {"Xhigh", "xhigh"}, {"Max", "max"}};
    static const Opt kGeneric[] = {{"Off", "off"}, {"Low", "low"}, {"Medium", "medium"},
                                   {"High", "high"}};
    std::string m = id;
    for (char& c : m) c = (char)std::tolower((unsigned char)c);
    const Opt* src = kGeneric;
    int n = 4;
    if (provider == PROV_GEMINI || m.find("gemini") != std::string::npos) {
        if (m.find("3.1-pro") != std::string::npos) { src = kGeminiPro; n = 2; }
        else { src = kGeminiLevel; n = 4; }
    } else if (provider == PROV_ANTHROPIC || m.find("claude") != std::string::npos) {
        if (m.find("-4-5") != std::string::npos) { src = kClaudeBudget; n = 5; }
        else if (m.find("sonnet-4-6") != std::string::npos) { src = kClaudeSonnet46; n = 3; }
        else if (m.find("-4-6") != std::string::npos) { src = kClaude46; n = 4; }
        else { src = kClaudeAdaptive; n = 5; }
    } else if (m.find("kimi-k3") != std::string::npos || m == "k3" ||
               m.rfind("k3-", 0) == 0) {
        src = kKimiK3; n = 3;
    } else if (m.rfind("gpt-6", 0) == 0 || m.rfind("gpt-7", 0) == 0 ||
               m.rfind("gpt-5.6", 0) == 0) {
        src = kGptModern; n = 5;
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
