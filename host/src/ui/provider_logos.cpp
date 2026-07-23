// provider_logos.cpp — see provider_logos.h. Baseline implementation: a per-provider brand
// color + ASCII mark table with a hashed fallback, and a rounded badge drawer. Extend the
// table as new providers appear in the OMP catalog.
#include "provider_logos.h"

#include <array>
#include <cstdint>
#include <unordered_map>

#include "ui_theme.h"

#include <cctype>
#include <filesystem>
#include <fstream>
#include <set>
#include <system_error>

#include <nlohmann/json.hpp>

#include "stb_image.h"
#include "gl_compat.h"
#include "core/app_state.h"

namespace ui {

namespace {

struct Brand {
    float col[3];
    const char* mark;
};

// Brand colors are approximate accents chosen to read on the frosted-glass panels — not exact
// trademark values. Keyed by the exact OMP provider id (auth_list_providers `id`, not label) —
// sibling plan/region variants of one brand (e.g. the xiaomi-token-plan-* trio) get their own
// entries so every row the bridge can return has an explicit, distinct badge.
const std::unordered_map<std::string, Brand>& brandTable() {
    static const std::unordered_map<std::string, Brand> t = {
        // -- Anthropic / OpenAI / Google / xAI (+ their auth/device/cloud variants) --
        {"anthropic",             {{0.85f, 0.47f, 0.30f}, "A"}},    // clay
        {"openai",                {{0.12f, 0.72f, 0.60f}, "O"}},    // teal
        {"openai-codex",          {{0.12f, 0.72f, 0.60f}, "Cx"}},
        {"openai-codex-device",   {{0.12f, 0.72f, 0.60f}, "Cd"}},
        {"azure",                 {{0.20f, 0.47f, 0.90f}, "Az"}},   // azure blue
        {"google",                {{0.26f, 0.52f, 0.96f}, "G"}},    // google blue
        {"gemini",                {{0.26f, 0.52f, 0.96f}, "G"}},
        {"google-gemini-cli",     {{0.30f, 0.58f, 0.90f}, "Gc"}},
        {"google-vertex",         {{0.20f, 0.45f, 0.85f}, "Gv"}},
        {"google-antigravity",    {{0.15f, 0.70f, 0.55f}, "Ag"}},   // antigravity teal-green
        {"xai",                   {{0.10f, 0.10f, 0.12f}, "X"}},    // near-black
        {"xai-oauth",             {{0.14f, 0.14f, 0.18f}, "Xo"}},
        // -- Moonshot / Kimi / DeepSeek / Zhipu / Z.AI family --
        {"kimi-code",             {{0.40f, 0.30f, 0.85f}, "K"}},    // moonshot violet
        {"moonshot",              {{0.40f, 0.30f, 0.85f}, "M"}},
        {"deepseek",              {{0.24f, 0.40f, 0.88f}, "D"}},
        {"zai",                   {{0.20f, 0.50f, 0.85f}, "Z"}},
        {"z-ai",                  {{0.20f, 0.50f, 0.85f}, "Z"}},
        {"zhipu",                 {{0.20f, 0.50f, 0.85f}, "Zp"}},
        {"zhipu-coding-plan",     {{0.20f, 0.50f, 0.85f}, "Zp"}},
        {"firepass",              {{0.95f, 0.40f, 0.60f}, "Fp"}},   // fireworks-hosted Kimi plan
        // -- Alibaba / Qwen --
        {"alibaba",               {{0.55f, 0.30f, 0.80f}, "Qw"}},
        {"alibaba-coding-plan",   {{0.55f, 0.30f, 0.80f}, "Qw"}},
        {"qwen",                  {{0.55f, 0.30f, 0.80f}, "Qw"}},
        {"qwen-portal",           {{0.60f, 0.32f, 0.78f}, "Qp"}},
        // -- Xiaomi MiMo family --
        {"xiaomi",                {{0.95f, 0.45f, 0.10f}, "Xm"}},
        {"xiaomi-token-plan-sgp", {{0.90f, 0.50f, 0.15f}, "Xs"}},
        {"xiaomi-token-plan-ams", {{0.90f, 0.55f, 0.20f}, "Xa"}},
        {"xiaomi-token-plan-cn",  {{0.85f, 0.45f, 0.12f}, "Xc"}},
        // -- MiniMax family --
        {"minimax",               {{0.85f, 0.20f, 0.30f}, "Mm"}},
        {"minimax-code",          {{0.90f, 0.20f, 0.25f}, "Mx"}},
        {"minimax-code-cn",       {{0.85f, 0.25f, 0.30f}, "Mc"}},
        // -- Inference / gateway providers --
        {"mistral",               {{0.93f, 0.53f, 0.13f}, "Mi"}},   // orange
        {"groq",                  {{0.90f, 0.30f, 0.20f}, "gq"}},
        {"together",              {{0.35f, 0.45f, 0.95f}, "T"}},
        {"fireworks",             {{0.95f, 0.35f, 0.55f}, "Fw"}},
        {"openrouter",            {{0.45f, 0.55f, 0.65f}, "OR"}},
        {"cerebras",              {{0.80f, 0.28f, 0.30f}, "C"}},
        {"baseten",               {{0.35f, 0.55f, 0.50f}, "B"}},
        {"bedrock",               {{0.90f, 0.55f, 0.15f}, "Aw"}},   // aws orange
        {"amazon-bedrock",        {{0.90f, 0.55f, 0.15f}, "Aw"}},
        {"aiml",                  {{0.30f, 0.60f, 0.80f}, "AI"}},
        {"aimlapi",               {{0.30f, 0.60f, 0.80f}, "Am"}},
        {"zenmux",                {{0.55f, 0.45f, 0.80f}, "Zx"}},
        {"nvidia",                {{0.46f, 0.73f, 0.13f}, "Nv"}},
        {"novita",                {{0.20f, 0.65f, 0.70f}, "No"}},
        {"huggingface",           {{0.85f, 0.65f, 0.15f}, "Hf"}},
        {"perplexity",            {{0.15f, 0.65f, 0.60f}, "Px"}},
        {"qianfan",               {{0.15f, 0.45f, 0.85f}, "Qf"}},
        {"venice",                {{0.75f, 0.30f, 0.65f}, "Vn"}},
        {"synthetic",             {{0.45f, 0.50f, 0.60f}, "Sy"}},
        {"nanogpt",               {{0.30f, 0.75f, 0.55f}, "Ng"}},
        {"wafer-serverless",      {{0.85f, 0.65f, 0.20f}, "Wf"}},
        {"coreweave",             {{0.90f, 0.40f, 0.20f}, "Cw"}},
        {"vercel-ai-gateway",     {{0.14f, 0.14f, 0.16f}, "Ve"}},
        {"cloudflare-ai-gateway", {{0.95f, 0.55f, 0.10f}, "Cf"}},
        {"litellm",               {{0.10f, 0.55f, 0.50f}, "Ll"}},
        {"kilo",                  {{0.60f, 0.35f, 0.85f}, "Ki"}},
        {"opencode-zen",          {{0.35f, 0.70f, 0.40f}, "Oz"}},
        {"opencode-go",           {{0.30f, 0.65f, 0.55f}, "Og"}},
        // -- Coding-platform / agent integrations --
        {"github-copilot",        {{0.50f, 0.40f, 0.90f}, "Co"}},
        {"cursor",                {{0.14f, 0.16f, 0.24f}, "Cu"}},
        {"devin",                 {{0.36f, 0.42f, 0.95f}, "De"}},
        {"gitlab-duo",            {{0.90f, 0.35f, 0.15f}, "Gd"}},   // gitlab orange
        {"gitlab-duo-agent",      {{0.95f, 0.45f, 0.20f}, "Ga"}},
        {"umans",                 {{0.50f, 0.70f, 0.30f}, "Um"}},
        {"sakana",                {{0.95f, 0.40f, 0.55f}, "Sk"}},
        // -- Search / tool providers --
        {"tavily",                {{0.20f, 0.55f, 0.90f}, "Tv"}},
        {"kagi",                  {{0.45f, 0.35f, 0.85f}, "Kg"}},
        {"parallel",              {{0.90f, 0.45f, 0.30f}, "Pl"}},
        // -- Local / self-hosted runtimes --
        {"ollama",                {{0.35f, 0.38f, 0.42f}, "Ol"}},
        {"ollama-cloud",          {{0.30f, 0.55f, 0.55f}, "Oc"}},
        {"lm-studio",             {{0.50f, 0.40f, 0.80f}, "Ls"}},
        {"llama.cpp",             {{0.40f, 0.40f, 0.45f}, "Lc"}},
        {"vllm",                  {{0.20f, 0.60f, 0.65f}, "Vl"}},
    };
    return t;
}

// Stable hashed hue for unknown providers so every row still gets a distinct badge.
const float* hashedColor(const std::string& id) {
    static thread_local float rgb[3];
    std::uint32_t h = 2166136261u;
    for (unsigned char c : id) { h ^= c; h *= 16777619u; }
    const float hue = (h % 360u) / 360.0f;
    ImGui::ColorConvertHSVtoRGB(hue, 0.55f, 0.75f, rgb[0], rgb[1], rgb[2]);
    return rgb;
}

// First non-space char of the id, uppercased — the last-resort mark.
const char* hashedMark(const std::string& id) {
    static thread_local char m[2] = {'?', '\0'};
    for (char c : id) {
        if (c == ' ' || c == '-' || c == '_') continue;
        m[0] = (c >= 'a' && c <= 'z') ? char(c - 32) : c;
        return m;
    }
    return "?";
}

// ---- runtime favicon layer: bridge fetch -> appdata cache -> GL texture ---------------------
// id -> brand domain. Only ids with a real, stable website are listed; the favicon service the
// bridge queries resolves the icon. Unknown / on-prem ids (ollama, lm-studio, llama.cpp, vllm)
// are intentionally absent -> they keep the drawn badge. Approximate domains are fine: a wrong
// or missing icon just falls back to the badge.
const std::unordered_map<std::string, std::string>& domainTable() {
    static const std::unordered_map<std::string, std::string> t = {
        {"anthropic", "anthropic.com"}, {"openai", "openai.com"},
        {"openai-codex", "openai.com"}, {"openai-codex-device", "openai.com"},
        {"azure", "azure.microsoft.com"}, {"google", "google.com"},
        {"gemini", "gemini.google.com"}, {"google-antigravity", "google.com"},
        {"google-gemini-cli", "google.com"}, {"google-vertex", "cloud.google.com"},
        {"xai", "x.ai"}, {"xai-oauth", "x.ai"}, {"kimi-code", "moonshot.cn"},
        {"moonshot", "moonshot.cn"}, {"deepseek", "deepseek.com"}, {"mistral", "mistral.ai"},
        {"groq", "groq.com"}, {"together", "together.ai"}, {"fireworks", "fireworks.ai"},
        {"openrouter", "openrouter.ai"}, {"cerebras", "cerebras.ai"}, {"baseten", "baseten.co"},
        {"bedrock", "aws.amazon.com"}, {"amazon-bedrock", "aws.amazon.com"},
        {"aiml", "aimlapi.com"}, {"aimlapi", "aimlapi.com"}, {"zhipu", "z.ai"},
        {"zhipu-coding-plan", "z.ai"}, {"zai", "z.ai"}, {"z-ai", "z.ai"},
        {"alibaba", "alibabacloud.com"}, {"alibaba-coding-plan", "alibabacloud.com"},
        {"qwen", "qwen.ai"}, {"qwen-portal", "qwen.ai"}, {"github-copilot", "github.com"},
        {"cursor", "cursor.com"}, {"devin", "devin.ai"}, {"gitlab-duo", "gitlab.com"},
        {"gitlab-duo-agent", "gitlab.com"}, {"minimax", "minimax.io"},
        {"minimax-code", "minimax.io"}, {"minimax-code-cn", "minimaxi.com"},
        {"xiaomi", "mi.com"}, {"xiaomi-token-plan-cn", "mi.com"},
        {"xiaomi-token-plan-sgp", "mi.com"}, {"xiaomi-token-plan-ams", "mi.com"},
        {"nvidia", "nvidia.com"}, {"novita", "novita.ai"}, {"huggingface", "huggingface.co"},
        {"perplexity", "perplexity.ai"}, {"qianfan", "cloud.baidu.com"}, {"venice", "venice.ai"},
        {"nanogpt", "nano-gpt.com"}, {"vercel-ai-gateway", "vercel.com"},
        {"cloudflare-ai-gateway", "cloudflare.com"}, {"litellm", "litellm.ai"},
        {"tavily", "tavily.com"}, {"kagi", "kagi.com"}, {"sakana", "sakana.ai"},
        {"cohere", "cohere.com"},
    };
    return t;
}

struct LogoTex { unsigned int tex = 0; int w = 0, h = 0; bool fetching = false; };
std::unordered_map<std::string, LogoTex> g_logoTex;  // GL thread only
std::set<std::string> g_logoReq;                     // ids drawn since last tick (GL thread)
int g_logoEnabled = -1;                              // -1 unknown, 0 off, 1 on

std::string logoCacheDir() { return appdataAppDir() + "/provider_logos"; }
std::string logoSafeId(const std::string& id) {
    std::string s; s.reserve(id.size());
    for (char c : id) s += std::isalnum((unsigned char)c) ? c : '_';
    return s;
}
std::string logoCachePath(const std::string& id) {
    return logoCacheDir() + "/" + logoSafeId(id) + ".png";
}

// Standard base64 decode (bridge returns unpadded-safe standard base64, no line breaks).
std::string b64dec(const std::string& in) {
    static const std::array<int, 256> rev = [] {
        std::array<int, 256> r; r.fill(-1);
        const char* tbl = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        for (int i = 0; i < 64; ++i) r[(unsigned char)tbl[i]] = i;
        return r;
    }();
    std::string out; out.reserve(in.size() * 3 / 4);
    int val = 0, bits = -8;
    for (unsigned char c : in) {
        if (c == '=') break;
        if (rev[c] < 0) continue;
        val = (val << 6) + rev[c]; bits += 6;
        if (bits >= 0) { out.push_back(char((val >> bits) & 0xFF)); bits -= 8; }
    }
    return out;
}

bool logoEnabled() {
    if (g_logoEnabled >= 0) return g_logoEnabled != 0;
    g_logoEnabled = 1;  // default on (user opted in)
    std::ifstream f(configPath());
    if (f) {
        try {
            nlohmann::json j; f >> j;
            if (j.contains("agent") && j["agent"].contains("provider_logos"))
                g_logoEnabled = j["agent"]["provider_logos"].get<bool>() ? 1 : 0;
        } catch (...) {}
    }
    return g_logoEnabled != 0;
}

}  // namespace

const float* providerBrandColor(const std::string& providerId) {
    auto it = brandTable().find(providerId);
    if (it != brandTable().end()) return it->second.col;
    return hashedColor(providerId);
}

const char* providerBrandMark(const std::string& providerId) {
    auto it = brandTable().find(providerId);
    if (it != brandTable().end()) return it->second.mark;
    return hashedMark(providerId);
}

void drawProviderLogo(ImDrawList* dl, ImVec2 pos, float side, const std::string& providerId) {
    if (!dl) return;
    // Real favicon (when enabled + fetched): register the id for the next tick and, if its
    // texture is ready, draw it rounded on a white backing. Otherwise fall through to the badge.
    if (logoEnabled()) {
        g_logoReq.insert(providerId);
        auto it = g_logoTex.find(providerId);
        if (it != g_logoTex.end() && it->second.tex) {
            const ImVec2 mx(pos.x + side, pos.y + side);
            const float rounding = side * 0.28f;
            dl->AddRectFilled(pos, mx, IM_COL32(255, 255, 255, 235), rounding);
            dl->AddImageRounded((ImTextureID)(intptr_t)it->second.tex, pos, mx,
                                ImVec2(0, 0), ImVec2(1, 1), IM_COL32(255, 255, 255, 255), rounding);
            return;
        }
    }
    const float* c = providerBrandColor(providerId);
    const ImU32 fill = ImGui::ColorConvertFloat4ToU32(ImVec4(c[0], c[1], c[2], 1.0f));
    // Slightly darker ring for definition on light panels.
    const ImU32 ring = ImGui::ColorConvertFloat4ToU32(ImVec4(c[0] * 0.7f, c[1] * 0.7f, c[2] * 0.7f, 1.0f));
    const ImVec2 mx(pos.x + side, pos.y + side);
    const float rounding = side * 0.28f;

    if (providerId == "xai") {
        // xAI's mark is just two bold crossing diagonals — reads cleanly even at badge size,
        // so draw the real stroke instead of a letter.
        dl->AddRectFilled(pos, mx, fill, rounding);
        dl->AddRect(pos, mx, ring, rounding, 0, 1.0f);
        const float pad = side * 0.26f;
        const float thickness = side * 0.14f;
        const ImU32 ink = IM_COL32(255, 255, 255, 235);
        dl->AddLine(ImVec2(pos.x + pad, pos.y + pad), ImVec2(mx.x - pad, mx.y - pad), ink, thickness);
        dl->AddLine(ImVec2(mx.x - pad, pos.y + pad), ImVec2(pos.x + pad, mx.y - pad), ink, thickness);
        return;
    }
    if (providerId == "google") {
        // Google's mark is recognizable purely from its four brand colors; draw them as a ring
        // on a white badge instead of a flat blue tile + letter.
        constexpr float kPi = 3.14159265358979323846f;
        dl->AddRectFilled(pos, mx, IM_COL32(255, 255, 255, 255), rounding);
        dl->AddRect(pos, mx, ring, rounding, 0, 1.0f);
        const ImVec2 center(pos.x + side * 0.5f, pos.y + side * 0.5f);
        const float radius = side * 0.30f;
        const float thickness = side * 0.17f;
        const ImU32 blue = IM_COL32(66, 133, 244, 255);
        const ImU32 red = IM_COL32(234, 67, 53, 255);
        const ImU32 yellow = IM_COL32(251, 188, 5, 255);
        const ImU32 green = IM_COL32(52, 168, 83, 255);
        dl->PathArcTo(center, radius, -kPi * 0.5f, kPi * 0.0f);
        dl->PathStroke(blue, 0, thickness);
        dl->PathArcTo(center, radius, kPi * 0.0f, kPi * 0.5f);
        dl->PathStroke(red, 0, thickness);
        dl->PathArcTo(center, radius, kPi * 0.5f, kPi * 1.0f);
        dl->PathStroke(green, 0, thickness);
        dl->PathArcTo(center, radius, kPi * 1.0f, kPi * 1.5f);
        dl->PathStroke(yellow, 0, thickness);
        return;
    }

    dl->AddRectFilled(pos, mx, fill, rounding);
    dl->AddRect(pos, mx, ring, rounding, 0, 1.0f);
    const char* mark = providerBrandMark(providerId);
    const ImVec2 ts = ImGui::CalcTextSize(mark);
    const ImU32 ink = IM_COL32(255, 255, 255, 235);
    dl->AddText(ImVec2(pos.x + (side - ts.x) * 0.5f, pos.y + (side - ts.y) * 0.5f), ink, mark);
}
bool providerLogosEnabled() { return logoEnabled(); }

void setProviderLogosEnabled(bool on) {
    g_logoEnabled = on ? 1 : 0;
    // Persist config.json agent.provider_logos (load-modify-write).
    nlohmann::json j = nlohmann::json::object();
    { std::ifstream f(configPath()); if (f) { try { f >> j; } catch (...) { j = nlohmann::json::object(); } } }
    if (!j.is_object()) j = nlohmann::json::object();
    j["agent"]["provider_logos"] = on;
    std::error_code ec; std::filesystem::create_directories(appdataAppDir(), ec);
    { std::ofstream o(configPath(), std::ios::trunc); if (o) o << j.dump(2); }
    if (!on) {
        // Drop cached textures so the list falls back to badges immediately (GL thread).
        for (auto& kv : g_logoTex)
            if (kv.second.tex) { GLuint t = kv.second.tex; glDeleteTextures(1, &t); }
        g_logoTex.clear();
    }
}

void providerLogosTick(AppState& st) {
    if (!logoEnabled()) { g_logoReq.clear(); return; }
    int budget = 3;  // fetch kicks per frame (throttle a first-open burst)
    for (const std::string& id : g_logoReq) {
        auto dit = domainTable().find(id);
        if (dit == domainTable().end()) continue;   // no known domain -> badge only
        LogoTex& lt = g_logoTex[id];
        if (lt.tex) continue;                        // already uploaded
        const std::string path = logoCachePath(id);
        std::error_code ec;
        if (std::filesystem::is_regular_file(path, ec)) {
            int w = 0, h = 0, n = 0;
            unsigned char* px = stbi_load(path.c_str(), &w, &h, &n, 4);
            if (px && w > 0 && h > 0) {
                GLuint tex = 0;
                glGenTextures(1, &tex);
                glBindTexture(GL_TEXTURE_2D, tex);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
                glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, px);
                glBindTexture(GL_TEXTURE_2D, 0);
                lt.tex = tex; lt.w = w; lt.h = h;
            }
            if (px) stbi_image_free(px);
            else std::filesystem::remove(path, ec);  // corrupt -> drop, refetch next session
            continue;
        }
        if (!lt.fetching && budget > 0) {
            lt.fetching = true; --budget;
            const std::string domain = dit->second;
            st.rpc.call("agent_provider_logo",
                        nlohmann::json{{"domain", domain}, {"size", 64}},
                        [path](const core::RpcResult& r) {
                // Off the GL thread: only touch disk (atomic tmp+rename). The next tick loads it.
                if (!r.ok || !r.result.is_object()) return;
                if (!r.result.value("ok", false)) return;
                const std::string b64 = r.result.value("b64", std::string());
                if (b64.empty()) return;
                const std::string bytes = b64dec(b64);
                if (bytes.size() < 64) return;
                std::error_code ec2;
                std::filesystem::create_directories(
                    std::filesystem::path(path).parent_path(), ec2);
                const std::string tmp = path + ".tmp";
                { std::ofstream o(tmp, std::ios::binary | std::ios::trunc);
                  if (!o) return; o.write(bytes.data(), (std::streamsize)bytes.size()); }
                std::filesystem::rename(tmp, path, ec2);
            });
        }
    }
    g_logoReq.clear();
}


}  // namespace ui
