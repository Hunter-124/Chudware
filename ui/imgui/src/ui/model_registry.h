// Copilot model catalog + capability flags (shared by composer + settings).
#pragma once

#include <string>
#include <vector>

namespace ui {

enum ProviderIndex {
    PROV_ANTHROPIC = 0,
    PROV_OPENAI = 1,
    PROV_LOCAL = 2,
    PROV_LLAMACPP = 3,
    PROV_GEMINI = 4,
    PROV_CLOUDCOMPAT = 5,
    PROV_COUNT = 6,
};

struct ModelInfo {
    int provider = PROV_GEMINI;
    std::string id;
    std::string label;
    bool vision = true;
    bool reasoning = true;
};

inline const char* providerConfigName(int provider) {
    switch (provider) {
        case PROV_ANTHROPIC: return "anthropic";
        case PROV_OPENAI: return "openai";
        case PROV_LOCAL: return "openai";
        case PROV_LLAMACPP: return "llamacpp";
        case PROV_GEMINI: return "gemini";
        case PROV_CLOUDCOMPAT: return "openai";
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

inline bool providerIsCloudCompat(int provider) { return provider == PROV_CLOUDCOMPAT; }

inline const char* providerCatKey(int provider) {
    switch (provider) {
        case PROV_GEMINI: return "gemini";
        case PROV_ANTHROPIC: return "anthropic";
        case PROV_OPENAI: return "openai";
        case PROV_LOCAL:
        case PROV_LLAMACPP: return "local";
        case PROV_CLOUDCOMPAT: return "cloud";
        default: return "other";
    }
}

inline const char* cloudServiceKey(const std::string& base_url) {
    if (base_url.find("openrouter.ai") != std::string::npos) return "openrouter";
    if (base_url.find("aliyuncs.com") != std::string::npos) return "alibaba";
    if (base_url.find("deepseek.com") != std::string::npos) return "deepseek";
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
    return "Cloud API";
}

inline const std::vector<ModelInfo>& builtinModels() {
    static const std::vector<ModelInfo> kModels = {
        {PROV_GEMINI, "gemini-2.5-pro", "Gemini 2.5 Pro", true, true},
        {PROV_GEMINI, "gemini-2.5-flash", "Gemini 2.5 Flash", true, true},
        {PROV_GEMINI, "gemini-2.0-flash", "Gemini 2.0 Flash", true, false},
        {PROV_ANTHROPIC, "claude-opus-4-8", "Claude Opus 4.8", true, true},
        {PROV_OPENAI, "gpt-4o", "GPT-4o", true, false},
        {PROV_OPENAI, "o3", "OpenAI o3", false, true},
    };
    return kModels;
}

struct LocalPreset {
    std::string id;
    std::string label;
    bool vision = false;
    std::string note;
    std::string baseUrl;
};

inline const std::vector<LocalPreset>& localPresets() {
    static const std::vector<LocalPreset> kPresets = {
        {"qwen2.5-coder-7b-instruct", "Qwen2.5-Coder 7B (ops/code)", false,
         "Code model; reliable JSON + tool-calling for emitting CAD ops.",
         "http://localhost:1234/v1"},
        {"qwen2.5-coder-32b-instruct", "Qwen2.5-Coder 32B (ops/code)", false,
         "Larger coder; best structured-op quality if your GPU fits it.",
         "http://localhost:1234/v1"},
        {"qwen2.5-14b-instruct", "Qwen2.5 14B (general)", false,
         "General model; solid planner/intake without a vision head.",
         "http://localhost:1234/v1"},
        {"qwen2.5-vl-7b-instruct", "Qwen2.5-VL 7B (vision review)", true,
         "Vision model; strong grounding on rendered viewport PNGs.",
         "http://localhost:1234/v1"},
        {"qwen2.5-vl-3b-instruct", "Qwen2.5-VL 3B (vision, light)", true,
         "Smaller vision judge for modest GPUs.",
         "http://localhost:1234/v1"},
        {"deepseek-r1-distill-qwen-32b", "DeepSeek-R1 Distill 32B (reasoning)", false,
         "Reasoning model; Seek-CAD local design loop.",
         "http://localhost:1234/v1"},
    };
    return kPresets;
}

inline const std::vector<LocalPreset>& cloudPresets() {
    static const std::string kDashScope = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1";
    static const std::string kDeepSeek = "https://api.deepseek.com/v1";
    static const std::string kOpenRouter = "https://openrouter.ai/api/v1";
    static const std::vector<LocalPreset> kPresets = {
        {"qwen/qwen3-coder:free", "OpenRouter · Qwen3-Coder (free)", false,
         "OpenRouter free tier — Qwen3 coder for op emission.", kOpenRouter},
        {"qwen/qwen3-coder", "OpenRouter · Qwen3-Coder (paid)", false,
         "OpenRouter paid — reliable Qwen3 coder.", kOpenRouter},
        {"qwen/qwen3-vl-32b-instruct", "OpenRouter · Qwen3-VL 32B (vision, paid)", true,
         "OpenRouter paid — Qwen3 vision model.", kOpenRouter},
        {"qwen/qwen3-max", "OpenRouter · Qwen3-Max (flagship, paid)", false,
         "OpenRouter paid — Qwen3 flagship.", kOpenRouter},
        {"deepseek/deepseek-chat", "OpenRouter · DeepSeek V3 (paid)", false,
         "OpenRouter paid — DeepSeek V3.", kOpenRouter},
        {"openai/gpt-oss-120b:free", "OpenRouter · GPT-OSS 120B (free)", false,
         "OpenRouter free tier — open-weight 120B.", kOpenRouter},
        {"qwen3-max", "Alibaba Qwen3-Max (flagship)", false,
         "Alibaba Cloud DashScope Qwen3 flagship.", kDashScope},
        {"qwen3-vl-plus", "Alibaba Qwen3-VL-Plus (vision)", true,
         "Alibaba Cloud DashScope Qwen3 vision model.", kDashScope},
        {"qwen3-coder-plus", "Alibaba Qwen3-Coder-Plus (ops/code)", false,
         "Alibaba Cloud DashScope Qwen3 code model.", kDashScope},
        {"deepseek-chat", "DeepSeek V3 (chat)", false,
         "Official DeepSeek API.", kDeepSeek},
        {"deepseek-reasoner", "DeepSeek R1 (reasoner)", false,
         "Official DeepSeek API reasoning model.", kDeepSeek},
    };
    return kPresets;
}

inline bool modelSupportsReasoning(int provider, const std::string& id) {
    for (const auto& m : builtinModels())
        if (m.provider == provider && m.id == id) return m.reasoning;
    if (provider == PROV_LOCAL || provider == PROV_LLAMACPP) return true;
    return false;
}

inline bool modelSupportsVision(int provider, const std::string& id) {
    for (const auto& m : builtinModels())
        if (m.provider == provider && m.id == id) return m.vision;
    return provider != PROV_LLAMACPP;
}

inline int defaultContextWindowForModel(int provider, const std::string& id) {
    std::string m = id;
    for (char& c : m) c = (char)std::tolower((unsigned char)c);
    auto has = [&](const char* sub) -> bool {
        if (!sub || !*sub) return false;
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
    if (has("qwen3-max") || has("qwen3-vl")) return 131072;
    if (has("qwen") || has("deepseek")) return 128000;
    switch (provider) {
        case PROV_GEMINI: return 1048576;
        case PROV_ANTHROPIC: return 200000;
        case PROV_OPENAI:
        case PROV_CLOUDCOMPAT: return 128000;
        default: return 8192;
    }
}

inline int reasoningEffortOptions(int provider, const std::string& id,
                                  const char** outLabels, const char** outValues, int maxN) {
    if (!modelSupportsReasoning(provider, id)) return 0;
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

}  // namespace ui
