// host/src/ui/model_registry.h — the copilot model catalog + capability flags.
//
// Header-only so both settings_modal.cpp and legacy_chat.cpp can share it without a
// build-file change. Drives the "model picker": which models exist per provider, whether
// a model supports a reasoning-effort control, and whether it can see screenshots (vision).
// Live availability (key present / endpoint reachable / which local models are loaded) is
// resolved at runtime by AppState's model probe (agent_list_models RPC); this file is the
// static knowledge the probe augments.
#pragma once

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
    PROV_COUNT = 6,
};

struct ModelInfo {
    int provider = PROV_GEMINI;
    std::string id;        // wire model id (config.json "model")
    std::string label;     // human label for the picker
    bool vision = true;    // can receive screenshots
    bool reasoning = true; // exposes a reasoning-effort knob
};

// config.json "provider" string <-> Settings::provider index. Local + OpenAI cloud both
// serialize as "openai" (local adds an openai.base_url); the index distinguishes them.
inline const char* providerConfigName(int provider) {
    switch (provider) {
        case PROV_ANTHROPIC: return "anthropic";
        case PROV_OPENAI: return "openai";
        case PROV_LOCAL: return "openai";
        case PROV_LLAMACPP: return "llamacpp";
        case PROV_GEMINI: return "gemini";
        case PROV_CLOUDCOMPAT: return "openai";  // cloud OpenAI-compatible via base_url
        default: return "openai";
    }
}

inline const char* providerLabel(int provider) {
    switch (provider) {
        case PROV_ANTHROPIC: return "Anthropic (Claude)";
        case PROV_OPENAI: return "OpenAI";
        case PROV_LOCAL: return "Local (OpenAI-compatible)";
        case PROV_LLAMACPP: return "llama.cpp (managed local)";
        case PROV_GEMINI: return "Google Gemini";
        case PROV_CLOUDCOMPAT: return "Cloud API (Qwen / DeepSeek / OpenAI-compatible)";
        default: return "?";
    }
}

inline bool providerIsCloud(int provider) {
    return provider == PROV_ANTHROPIC || provider == PROV_OPENAI || provider == PROV_GEMINI ||
           provider == PROV_CLOUDCOMPAT;
}

// True for the cloud OpenAI-compatible API slot — the one cloud provider that also needs a
// user-editable base URL (Alibaba DashScope, DeepSeek, etc.). Refused under offline (ITAR) mode.
inline bool providerIsCloudCompat(int provider) { return provider == PROV_CLOUDCOMPAT; }

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
        // OpenAI cloud.
        {PROV_OPENAI, "gpt-4o", "GPT-4o", true, false},
        {PROV_OPENAI, "o3", "OpenAI o3", false, true},
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
// Cloud's Qwen API; api.deepseek.com = the official DeepSeek API.
inline const std::vector<LocalPreset>& cloudPresets() {
    static const std::string kDashScope = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1";
    static const std::string kDeepSeek = "https://api.deepseek.com/v1";
    static const std::vector<LocalPreset> kPresets = {
        // Alibaba Cloud (DashScope) — Qwen flagship + vision + coder.
        {"qwen-max", "Alibaba Qwen-Max (flagship)", false,
         "Alibaba Cloud DashScope. Strong general model; needs a DashScope API key.", kDashScope},
        {"qwen-plus", "Alibaba Qwen-Plus (balanced)", false,
         "Alibaba Cloud DashScope. Cheaper/faster than Max.", kDashScope},
        {"qwen-vl-max", "Alibaba Qwen-VL-Max (vision)", true,
         "Alibaba Cloud DashScope vision model — great as the review judge.", kDashScope},
        {"qwen-coder-plus", "Alibaba Qwen-Coder-Plus (ops/code)", false,
         "Alibaba Cloud DashScope code model for structured op emission.", kDashScope},
        // DeepSeek official API.
        {"deepseek-chat", "DeepSeek V3 (chat)", false,
         "Official DeepSeek API. Strong general/coding model.", kDeepSeek},
        {"deepseek-reasoner", "DeepSeek R1 (reasoner)", false,
         "Official DeepSeek API reasoning model (R1).", kDeepSeek},
    };
    return kPresets;
}

// Does (provider, model id) expose a reasoning-effort knob? Falls back to the provider
// default for unknown ids (local models route through gemma-style reasoning -> true).
inline bool modelSupportsReasoning(int provider, const std::string& id) {
    for (const auto& m : builtinModels())
        if (m.provider == provider && m.id == id) return m.reasoning;
    if (provider == PROV_LOCAL || provider == PROV_LLAMACPP) return true;  // gemma reasons
    return false;
}

inline bool modelSupportsVision(int provider, const std::string& id) {
    for (const auto& m : builtinModels())
        if (m.provider == provider && m.id == id) return m.vision;
    return provider != PROV_LLAMACPP;  // local default vision-on unless managed llama.cpp
}

// The reasoning-effort levels exposed in the UI, in config string form.
inline const char* const* reasoningEffortValues(int* n) {
    static const char* kVals[] = {"off", "low", "medium", "high"};
    if (n) *n = 4;
    return kVals;
}
inline const char* const* reasoningEffortLabels(int* n) {
    static const char* kLabels[] = {"Off", "Low", "Medium", "High"};
    if (n) *n = 4;
    return kLabels;
}
inline int reasoningEffortToIndex(const std::string& v) {
    int n = 0; auto vals = reasoningEffortValues(&n);
    for (int i = 0; i < n; ++i) if (v == vals[i]) return i;
    return 3;  // default High
}

}  // namespace ui
