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
    PROV_COUNT = 5,
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
        default: return "?";
    }
}

inline bool providerIsCloud(int provider) {
    return provider == PROV_ANTHROPIC || provider == PROV_OPENAI || provider == PROV_GEMINI;
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
        // OpenAI cloud.
        {PROV_OPENAI, "gpt-4o", "GPT-4o", true, false},
        {PROV_OPENAI, "o3", "OpenAI o3", false, true},
    };
    return kModels;
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
