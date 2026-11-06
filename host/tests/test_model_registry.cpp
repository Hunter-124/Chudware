// test_model_registry.cpp — ctest for ui::isLegacyGenerationModel (header-only, no GL/ImGui).
//
// Policy under test: Claude older than 5.1 and every OpenAI model older than GPT-6 (gpt-3.5/4*/
// 4o/4.1/5/5.x, codex variants, o-series) is never offered in a model picker, on any provider
// route; Claude >= 5.1 and GPT >= 6 always are, and unversioned ids are never hidden.

#include "ui/model_registry.h"

#include <cstdio>
#include <string>

static int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

static void test_hidden() {
    const char* hidden[] = {
        "claude-opus-4-8", "claude-sonnet-4-5-20250929", "claude-haiku-4-5",
        "claude-3-5-sonnet-20241022", "claude-3-opus-20240229", "claude-3.7-sonnet",
        "claude-sonnet-5", "claude-opus-5", "claude-fable-5", "claude-mythos-5",
        "claude-opus-4-20250514", "claude-v2", "claude-instant-1.2",
        "anthropic/claude-sonnet-4.6", "cursor/claude-opus-4-8",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "CLAUDE-SONNET-4-5",
        "gpt-3.5-turbo", "gpt-4", "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4-turbo",
        "gpt-5", "gpt-5-codex", "gpt-5.1-codex-max", "gpt-5.6-sol", "openai/gpt-4o",
        "chatgpt-4o-latest", "gpt4o",
        "o1", "o1-preview", "o3", "o3-mini", "o4-mini", "openai/o3", "codex-mini-latest",
    };
    for (const char* id : hidden) {
        if (!ui::isLegacyGenerationModel(id)) {
            std::printf("FAIL: %s should be hidden\n", id);
            ++g_failures;
        }
    }
}

static void test_visible() {
    const char* visible[] = {
        "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-1",
        "claude-sonnet-5.1", "claude-5-1-opus", "claude-opus-6", "claude-opus-6-1",
        "anthropic/claude-opus-5.5", "claude-sonnet-5-5-20260101",
        "gpt-6-sol", "gpt-6-luna", "gpt-6.1-luna", "gpt-7-sol", "openai/gpt-6-sol",
        "gpt-oss-120b", "openai/gpt-oss-120b:free", "gpt-image-1",
        "composer-2.5", "gemini-3.5-flash", "grok-4.5", "kimi-k3", "k3", "qwen3-max",
        "deepseek-chat", "deepseek-reasoner", "omni-moderation-latest", "local-gguf",
    };
    for (const char* id : visible) {
        if (ui::isLegacyGenerationModel(id)) {
            std::printf("FAIL: %s should be offered\n", id);
            ++g_failures;
        }
    }
}

static void test_static_catalog_is_offerable() {
    for (const auto& m : ui::builtinModels())
        if (ui::isLegacyGenerationModel(m.id)) {
            std::printf("FAIL: static catalog row %s is policy-hidden\n", m.id.c_str());
            ++g_failures;
        }
}

int main() {
    test_hidden();
    test_visible();
    test_static_catalog_is_offerable();
    if (g_failures) {
        std::printf("test_model_registry: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_model_registry: all checks passed\n");
    return 0;
}
