// test_agent.cpp — unit tests for the agent provider layer (no network).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "environment_test_support.h"
#include "agent/auth.h"

#include "agent/providers.h"
#include "agent/config.h"
#include "agent/ask.h"
#include "agent/pm_assist.h"


using namespace chudware::agent;

namespace {
struct ScratchConfig {
    std::filesystem::path root;
    std::optional<std::string> previous;
    ScratchConfig() {
        static std::atomic<unsigned> seq{0};
        root = std::filesystem::temp_directory_path() /
               ("chudware-agent-config-" +
                std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()) +
                "-" + std::to_string(seq++));
        if (!std::filesystem::create_directory(root))
            throw std::runtime_error("could not create isolated test directory");
        const char* key = chudware::test::appDataEnvironmentVariable();
        if (const char* old = std::getenv(key)) previous = old;
        if (chudware::test::setEnvironment(key, root.string().c_str()) != 0)
            throw std::runtime_error("could not isolate test configuration");
    }
    ~ScratchConfig() {
        chudware::test::setOrUnsetEnvironment(chudware::test::appDataEnvironmentVariable(),
                                               previous ? previous->c_str() : nullptr);
        std::error_code ec;
        std::filesystem::remove_all(root, ec);
    }
    void write(const json& cfg) const {
        std::filesystem::create_directories(root / "Chudware");
        std::ofstream file(root / "Chudware" / "config.json", std::ios::binary | std::ios::trunc);
        if (!(file << cfg.dump())) throw std::runtime_error("could not write test configuration");
    }
};

struct FakeSubscriptionRefresh {
    int calls = 0;
    FakeSubscriptionRefresh() {
        auth::set_http_post_for_testing(
            [this](const std::string&, const std::vector<std::pair<std::string, std::string>>&,
                   const std::string&, int) {
                ++calls;
                return HttpResponse{500, "{}", "test blocked refresh"};
            });
    }
    ~FakeSubscriptionRefresh() { auth::set_http_post_for_testing(nullptr); }
};
}  // namespace


TEST_CASE("base64 standard encoding") {
    CHECK(base64_encode("") == "");
    CHECK(base64_encode("f") == "Zg==");
    CHECK(base64_encode("fo") == "Zm8=");
    CHECK(base64_encode("foo") == "Zm9v");
    CHECK(base64_encode("foob") == "Zm9vYg==");
    CHECK(base64_encode("fooba") == "Zm9vYmE=");
    CHECK(base64_encode("foobar") == "Zm9vYmFy");
    // bytes that exercise '+' and '/' in the standard alphabet.
    std::string raw;
    raw.push_back((char)0xFB);
    raw.push_back((char)0xFF);
    raw.push_back((char)0xBF);
    CHECK(base64_encode(raw) == "+/+/");
}

TEST_CASE("base url normalization") {
    CHECK(openai_normalize_base_url("") == "");
    CHECK(openai_normalize_base_url("http://localhost:1234") == "http://localhost:1234/v1");
    CHECK(openai_normalize_base_url("http://localhost:1234/") == "http://localhost:1234/v1");
    CHECK(openai_normalize_base_url("http://localhost:1234/v1") == "http://localhost:1234/v1");
    CHECK(openai_normalize_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1");
    // no scheme -> unchanged (urlparse gives no netloc).
    CHECK(openai_normalize_base_url("localhost:1234") == "localhost:1234");
}

TEST_CASE("anthropic body shape is model-aware") {
    json messages = json::array();
    messages.push_back(user_message(json::array({text_part("hello")})));

    // Current adaptive flagship: request shape and default effort.
    {
        BuiltRequest r = anthropic_build_request("claude-opus-5-5", "sys", messages, 8000);
        CHECK(r.url == "https://api.anthropic.com/v1/messages");
        CHECK(r.body_json["model"] == "claude-opus-5-5");
        CHECK(r.body_json["max_tokens"] == 8000);
        CHECK(r.body_json["system"] == "sys");
        CHECK(r.body_json["thinking"]["type"] == "adaptive");
        CHECK(r.body_json["output_config"]["effort"] == "high");
        CHECK(r.body_json["messages"][0]["role"] == "user");
        CHECK(r.body_json["messages"][0]["content"][0]["type"] == "text");
        CHECK(r.body_json["messages"][0]["content"][0]["text"] == "hello");
        // effort beta header only when output_config.effort is present.
        bool has_beta = false;
        for (const auto& h : r.headers)
            if (h.first == "anthropic-beta" && h.second == "effort-2025-11-24") has_beta = true;
        CHECK(has_beta);
    }

    // Sonnet 4.5: budget_tokens ONLY — no output_config.effort (API 400s it).
    {
        BuiltRequest r =
            anthropic_build_request("claude-sonnet-4-5-20250929", "sys", messages, 8000, "medium");
        CHECK(anthropic_thinking_mode_for_model("claude-sonnet-4-5-20250929") == "budget");
        CHECK(r.body_json["thinking"]["type"] == "enabled");
        CHECK(r.body_json["thinking"]["budget_tokens"] == 4096);
        CHECK_FALSE(r.body_json.contains("output_config"));
        bool has_beta = false;
        for (const auto& h : r.headers)
            if (h.first == "anthropic-beta") has_beta = true;
        CHECK_FALSE(has_beta);
    }

    // Haiku 4.5: same budget-only path.
    {
        BuiltRequest r =
            anthropic_build_request("claude-haiku-4-5", "sys", messages, 8000, "low");
        CHECK(anthropic_thinking_mode_for_model("claude-haiku-4-5") == "budget");
        CHECK(r.body_json["thinking"]["type"] == "enabled");
        CHECK(r.body_json["thinking"]["budget_tokens"] == 1024);
        CHECK_FALSE(r.body_json.contains("output_config"));
    }

    // Opus 4.5: budget_tokens + effort.
    {
        BuiltRequest r =
            anthropic_build_request("claude-opus-4-5", "sys", messages, 8000, "high");
        CHECK(anthropic_thinking_mode_for_model("claude-opus-4-5") == "budget_effort");
        CHECK(r.body_json["thinking"]["type"] == "enabled");
        CHECK(r.body_json["thinking"].contains("budget_tokens"));
        CHECK(r.body_json["output_config"]["effort"] == "high");
        bool has_beta = false;
        for (const auto& h : r.headers)
            if (h.first == "anthropic-beta" && h.second == "effort-2025-11-24") has_beta = true;
        CHECK(has_beta);
    }

    // Adaptive honors reasoning_effort (not pinned high).
    {
        BuiltRequest r =
            anthropic_build_request("claude-sonnet-5", "sys", messages, 8000, "low");
        CHECK(anthropic_thinking_mode_for_model("claude-sonnet-5") == "adaptive");
        CHECK(r.body_json["thinking"]["type"] == "adaptive");
        CHECK(r.body_json["output_config"]["effort"] == "low");
        BuiltRequest off =
            anthropic_build_request("claude-sonnet-5-5", "sys", messages, 8000, "off");
        CHECK(off.body_json["output_config"]["effort"] == "low");
    }
}

TEST_CASE("concise_http_error: non-429 extracts error.message") {
    std::string body =
        R"({"type":"error","error":{"type":"invalid_request_error","message":"This model does not support the effort parameter"}})";
    std::string e = concise_http_error("anthropic", 400, body);
    CHECK(e.find("HTTP 400") != std::string::npos);
    CHECK(e.find("effort parameter") != std::string::npos);
    // Not a raw JSON dump of the whole body on the last line.
    CHECK(e.find("invalid_request_error") == std::string::npos);
}

TEST_CASE("concise_http_error: 429 quota becomes one actionable line") {
    // The real body Gemini returned when the free-tier daily quota (20/day) was exhausted.
    std::string body =
        R"({"error":{"code":429,"message":"You exceeded your current quota, please check your )"
        R"(plan and billing details. For more information on this error, head to: https://x.","status")"
        R"(":"RESOURCE_EXHAUSTED","details":[{"@type":"type.googleapis.com/google.rpc.QuotaFailure",)"
        R"("violations":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}})";
    std::string e = concise_http_error("gemini", 429, body);
    // One line (no embedded newline → the run timeline shows the whole thing, not a bare "}").
    CHECK(e.find('\n') == std::string::npos);
    CHECK(e.find("daily quota") != std::string::npos);   // classified as the hard daily quota
    CHECK(e.find("local model") != std::string::npos);   // actionable advice present
    CHECK(e.find("billing") != std::string::npos);
    // The verbose URL tail is dropped (only the first sentence of the provider message is kept).
    CHECK(e.find("https://") == std::string::npos);

    // A non-429 error is passed through verbatim (still useful, no special-casing).
    CHECK(concise_http_error("openai", 500, "boom") == "openai HTTP 500: boom");
}

TEST_CASE("a vendor content refusal is not a transport failure") {
    // Live 2026-08-26: Anthropic refused a machinable pellet-press design "(bio)". The
    // whole run reported a generic provider error, so the retry budget kept paying to
    // re-ask a vendor that refuses the same prompt every time.
    const std::string refused =
        std::string("anthropic ") + kProviderRefusedMarker + " (bio): Refusal (bio)";
    CHECK(is_provider_refusal(refused));
    // …and it survives the "provider error: " reason prefix the stage actions add.
    CHECK(is_provider_refusal("provider error: " + refused));

    const std::string human = humanize_provider_error(refused);
    CHECK(human.find("declined this request on content policy") != std::string::npos);
    // The transport branch matches the bare substring "refused"; a refusal must never be
    // reported as a network fault, which would send the user to check their connection.
    CHECK(human.find("Could not reach") == std::string::npos);

    // The converse: an actual connection failure is never read as a policy decision.
    CHECK_FALSE(is_provider_refusal("ollama: connection refused"));
    CHECK(humanize_provider_error("ollama: connection refused to 127.0.0.1")
              .find("Could not reach") != std::string::npos);
}

TEST_CASE("cloud OpenAI-compatible (DashScope/DeepSeek): offline ITAR refuses, online resolves") {
    // The cloud Qwen/DeepSeek slot serializes as provider "openai" + a non-loopback base_url.
    auto cfg = [](bool offline) {
        return json{{"provider", "openai"},
                    {"model", "qwen-vl-max"},
                    {"api_key", "sk-test"},
                    {"offline_mode", offline},
                    {"openai",
                     {{"base_url", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
                      {"vision", true}}}};
    };
    // Offline (ITAR) mode => refused before any network use.
    json off = resolve_provider(std::string("openai"), cfg(true), json::object());
    REQUIRE(off.is_object());
    CHECK(off.contains("error"));
    // Online => resolves to a cloud (NOT offline-safe) descriptor with vision on.
    json on = resolve_provider(std::string("openai"), cfg(false), json::object());
    REQUIRE(on.is_object());
    CHECK_FALSE(on.contains("error"));
    CHECK(on.value("model", std::string()) == "qwen-vl-max");
    CHECK(on.value("supports_vision", false) == true);
    CHECK(on.value("offline_safe", true) == false);  // cloud host => never offline-safe

    // A LOCAL open-weight endpoint stays allowed under the SAME offline flag (the user's policy:
    // local fine under ITAR, only the cloud Alibaba/DeepSeek APIs are blocked).
    json local = resolve_provider(
        std::string("openai"),
        json{{"provider", "openai"}, {"model", "qwen2.5-vl-7b-instruct"}, {"offline_mode", true},
             {"openai", {{"base_url", "http://localhost:1234/v1"}, {"vision", true}}}},
        json::object());
    REQUIRE(local.is_object());
    CHECK_FALSE(local.contains("error"));
    CHECK(local.value("offline_safe", false) == true);
}

TEST_CASE("unsupported OMP identities are rejected before subscription refresh") {
    ScratchConfig scratch;
    FakeSubscriptionRefresh refresh;
    const json cfg{{"offline_mode", false},
                   {"subscription_auth",
                    {{"openai-codex", {{"access_token", "fake-expired"},
                                       {"refresh_token", "fake-refresh"},
                                       {"expires_at_ms", 0},
                                       {"account_id", "fake-account"}}}}}};
    scratch.write(cfg);
    json native_cfg = cfg;
    native_cfg["offline_mode"] = true;
    for (const std::string id : {"openai-codex", "codex-sub", "codex"}) {
        const json ambient = resolve_provider(id, native_cfg, json::object());
        REQUIRE(ambient.contains("error"));
        CHECK(ambient["error"].get<std::string>().find(id) != std::string::npos);
    }
    CHECK(refresh.calls == 0);
}

TEST_CASE("gemini thinking budget leaves room for the answer") {
    json messages = json::array();
    messages.push_back(user_message(json::array({text_part("hi")})));
    // Regression: Gemini 2.5 counts thinking tokens against maxOutputTokens. The output cap must
    // exceed the thinking budget or the model burns it all thinking and returns EMPTY content
    // (finishReason MAX_TOKENS) -> every stage fails with "no JSON ops in reply".
    auto out_cap = [&](const std::string& effort, int max_tokens) {
        BuiltRequest r =
            gemini_build_request("gemini-2.5-flash", "sys", messages, max_tokens, effort);
        return std::pair<int, int>{
            r.body_json["generationConfig"]["maxOutputTokens"].get<int>(),
            r.body_json["generationConfig"].contains("thinkingConfig")
                ? r.body_json["generationConfig"]["thinkingConfig"]["thinkingBudget"].get<int>()
                : 0};
    };
    // high effort (thinkingBudget 24576) used to cap output at 12288 -> no answer room.
    auto [cap_hi, think_hi] = out_cap("high", 12288);
    CHECK(think_hi == 24576);
    CHECK(cap_hi > think_hi);
    // medium effort (thinkingBudget 8192) used to EQUAL an 8192 cap -> zero answer room.
    auto [cap_med, think_med] = out_cap("medium", 8192);
    CHECK(think_med == 8192);
    CHECK(cap_med > think_med);
    // A saved "off" selects minimal budget (2.5 Pro cannot disable thinking).
    auto [cap_off, think_off] = out_cap("off", 8192);
    CHECK(think_off == 128);
    CHECK(cap_off > 8192);
}

TEST_CASE("gemini level models do not receive a legacy thinking budget") {
    json messages = json::array({user_message(json::array({text_part("hi")}))});
    BuiltRequest flash = gemini_build_request("gemini-3.5-flash", "sys", messages, 8192,
                                               "medium");
    CHECK(flash.body_json["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "MEDIUM");
    CHECK_FALSE(flash.body_json["generationConfig"]["thinkingConfig"].contains("thinkingBudget"));
    BuiltRequest pro = gemini_build_request("gemini-3.1-pro-preview", "sys", messages, 8192,
                                             "medium");
    CHECK(pro.body_json["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "HIGH");
}

TEST_CASE("openai message text-join branch") {
    json parts = json::array({text_part("a"), text_part("b")});
    json m = openai_message("user", parts);
    CHECK(m["content"] == "a\n\nb");
}

TEST_CASE("message parts tolerate null text field") {
    // Resolved provider descriptors may carry text:null; value() would throw type_error.302.
    json null_text = json{{"type", "text"}, {"text", nullptr}};
    CHECK(anthropic_block(null_text)["text"] == "");
    CHECK(openai_content(null_text)["text"] == "");
    CHECK(gemini_part(null_text)["text"] == "");
    json parts = json::array({null_text, text_part("ok")});
    CHECK(openai_message("user", parts)["content"] == "\n\nok");
}

TEST_CASE("anthropic parse concat") {
    json resp = {{"content", json::array({
                                 {{"type", "thinking"}, {"thinking", "reasoning"}},
                                 {{"type", "text"}, {"text", "hello "}},
                                 {{"type", "text"}, {"text", "world"}},
                             })}};
    ParsedReply pr = anthropic_parse_response(resp);
    CHECK(pr.text == "hello world");
    CHECK(pr.thinking == "reasoning");
}

TEST_CASE("openai parse") {
    json resp = {{"choices", json::array({{{"message", {{"content", "hi"}}}}})}};
    CHECK(openai_parse_response(resp).text == "hi");
    json empty = {{"choices", json::array()}};
    CHECK(openai_parse_response(empty).text == "");

    // Reasoning model with a real answer: content wins, reasoning is kept as thinking.
    json reason = {{"choices", json::array({{{"message",
                   {{"content", "answer"}, {"reasoning_content", "thought"}}}}})}};
    ParsedReply pr = openai_parse_response(reason);
    CHECK(pr.text == "answer");
    CHECK(pr.thinking == "thought");

    // Reasoning model that emitted ONLY reasoning (empty content) -> text stays EMPTY:
    // extraction must see the model's actions, never its scratchpad (no CoT->text
    // fallback — a JSON-ish blob the model is mulling over is not an answer).
    json only_reason = {{"choices", json::array({{{"message",
                        {{"content", ""}, {"reasoning_content", "{\"ops\":[]}"}}}}})}};
    ParsedReply onlyr = openai_parse_response(only_reason);
    CHECK(onlyr.text == "");
    CHECK(onlyr.thinking == "{\"ops\":[]}");

    // The `reasoning` spelling (some servers) is also captured — as thinking, not text.
    json alt = {{"choices", json::array({{{"message", {{"reasoning", "r"}}}}})}};
    ParsedReply altr = openai_parse_response(alt);
    CHECK(altr.text == "");
    CHECK(altr.thinking == "r");
}

TEST_CASE("mock provider repeats last") {
    MockProvider mp({"a", "b"});
    CHECK(mp.complete() == "a");
    CHECK(mp.complete() == "b");
    CHECK(mp.complete() == "b");
}

TEST_CASE("openai_reasoning_patch: per-provider/model reasoning-effort gating") {
    // off / empty -> omit everywhere (model uses its own default).
    CHECK(openai_reasoning_patch("openai", "gpt-5.6", "", "off").empty());
    CHECK(openai_reasoning_patch("openai", "gpt-5.6", "", "").empty());
    // OpenAI reasoning models pass low/medium/high; non-reasoning gpt-4* omit.
    CHECK(openai_reasoning_patch("openai", "gpt-5.6", "https://api.openai.com/v1", "high")
              .value("reasoning_effort", std::string()) == "high");
    CHECK(openai_reasoning_patch("openai", "gpt-6-sol", "https://api.openai.com/v1", "max")
              .value("reasoning_effort", std::string()) == "max");
    CHECK(openai_reasoning_patch("openai", "gpt-5.6-sol", "", "xhigh")
              .value("reasoning_effort", std::string()) == "xhigh");
    CHECK(openai_reasoning_patch("openai", "o3", "https://api.openai.com/v1", "medium")
              .value("reasoning_effort", std::string()) == "medium");
    CHECK(openai_reasoning_patch("openai", "gpt-4.1", "https://api.openai.com/v1", "high").empty());
    // xAI grok-4.5 passes low/medium/high; the original grok-4 has no knob.
    CHECK(openai_reasoning_patch("xai", "grok-4.5", "https://api.x.ai/v1", "low")
              .value("reasoning_effort", std::string()) == "low");
    CHECK(openai_reasoning_patch("xai", "grok-4", "https://api.x.ai/v1", "high").empty());
    // Moonshot kimi-k3 takes low|high|max and HONOURS the caller — it used to be
    // hardcoded to "max" regardless of the user's setting, so the dial did nothing.
    // There is no medium rung, so medium maps to high (the nearest real rung above
    // low). kimi-k2.6 omits reasoning_effort entirely (it uses `thinking`).
    CHECK(openai_reasoning_patch("moonshot", "kimi-k3", "https://api.moonshot.ai/v1", "low")
              .value("reasoning_effort", std::string()) == "low");
    CHECK(openai_reasoning_patch("moonshot", "kimi-k3", "https://api.moonshot.ai/v1", "medium")
              .value("reasoning_effort", std::string()) == "high");
    CHECK(openai_reasoning_patch("moonshot", "kimi-k3", "https://api.moonshot.ai/v1", "high")
              .value("reasoning_effort", std::string()) == "high");
    CHECK(openai_reasoning_patch("moonshot", "kimi-k3", "https://api.moonshot.ai/v1", "max")
              .value("reasoning_effort", std::string()) == "max");
    // Same through the model-name fallback arm (bare provider / empty base_url).
    CHECK(openai_reasoning_patch("", "kimi-k3", "", "max")
              .value("reasoning_effort", std::string()) == "max");
    CHECK(openai_reasoning_patch("moonshot", "kimi-k2.6", "https://api.moonshot.ai/v1", "high")
              .empty());
    // DeepSeek reasoner/v4 -> "high" only; deepseek-chat omit.
    CHECK(openai_reasoning_patch("openai", "deepseek-reasoner", "https://api.deepseek.com/v1", "low")
              .value("reasoning_effort", std::string()) == "high");
    CHECK(openai_reasoning_patch("openai", "deepseek-chat", "https://api.deepseek.com/v1", "high")
              .empty());
    // Alibaba Qwen (DashScope): hybrid thinking is a top-level enable_thinking bool.
    CHECK(openai_reasoning_patch("openai", "qwen3-max",
              "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "high")
              .value("enable_thinking", false) == true);
    // OpenRouter -> unified reasoning object.
    {
        json p = openai_reasoning_patch("openai", "qwen/qwen3-max",
                                        "https://openrouter.ai/api/v1", "medium");
        REQUIRE(p.contains("reasoning"));
        CHECK(p["reasoning"].value("effort", std::string()) == "medium");
    }
    // Local endpoint -> omit even for a reasoning-capable id.
    CHECK(openai_reasoning_patch("openai", "gpt-5", "http://localhost:1234/v1", "high").empty());
}

TEST_CASE("moonshot_normalize_model: kimi-k2.7 -> valid wire id, k3 aliases fold") {
    // Live 404: the bare `kimi-k2.7` is rejected ("Not found the model kimi-k2.7"); the official
    // moonshot.ai wire id is `kimi-k2.7-code`.
    CHECK(moonshot_normalize_model("kimi-k2.7") == "kimi-k2.7-code");
    // kimi-k3 stays kimi-k3; the capped/aliased profiles fold to the bare wire id.
    CHECK(moonshot_normalize_model("kimi-k3") == "kimi-k3");
    CHECK(moonshot_normalize_model("kimi-k3-256k") == "kimi-k3");
    CHECK(moonshot_normalize_model("moonshotai/kimi-k3") == "kimi-k3");
    // Already-valid wire ids pass through untouched — never downgraded to the 404 id.
    CHECK(moonshot_normalize_model("kimi-k2.7-code") == "kimi-k2.7-code");
    CHECK(moonshot_normalize_model("kimi-k2.6") == "kimi-k2.6");
}

// --------------------------------------------------------------------------- redaction (WS3)
TEST_CASE("compliance_mode normalization") {
    CHECK(compliance_mode(json::object()) == "off");
    CHECK(compliance_mode(json{{"compliance_mode", "off"}}) == "off");
    CHECK(compliance_mode(json{{"compliance_mode", "REDACT"}}) == "redact");
    CHECK(compliance_mode(json{{"compliance_mode", "local-only"}}) == "local_only");
    CHECK(compliance_mode(json{{"compliance_mode", "localonly"}}) == "local_only");
    CHECK(compliance_mode(json{{"compliance_mode", "garbage"}}) == "off");
    CHECK(local_only_enabled(json{{"compliance_mode", "local_only"}}));
    CHECK(local_only_enabled(json{{"offline_mode", true}}));
    CHECK_FALSE(local_only_enabled(json{{"compliance_mode", "redact"}}));
}

TEST_CASE("redaction scrub: dimension + part number round-trip") {
    RedactionMap map;
    const std::string src =
        "Machine the boss to 12.345 mm in 6061-T6 per P/N ABC-123, tol ±0.02 mm, bore H7/g6.";
    std::string scrubbed = redaction_scrub_text(src, map);
    // Controlled values never leave in cleartext.
    CHECK(scrubbed.find("12.345") == std::string::npos);
    CHECK(scrubbed.find("ABC-123") == std::string::npos);
    CHECK(scrubbed.find("6061-T6") == std::string::npos);
    CHECK(scrubbed.find("±0.02") == std::string::npos);
    CHECK(scrubbed.find("H7/g6") == std::string::npos);
    // Stable placeholders went in their place.
    CHECK(scrubbed.find("DIM_") != std::string::npos);
    CHECK(scrubbed.find("MAT_1") != std::string::npos);
    CHECK(scrubbed.find("PN_1") != std::string::npos);
    // Placeholders are stable: the same original maps to the same token on a later scrub.
    std::string again = redaction_scrub_text("recheck 12.345 mm and P/N ABC-123", map);
    CHECK(again.find("12.345") == std::string::npos);
    CHECK(again.find("ABC-123") == std::string::npos);
    CHECK(redaction_rehydrate_text(again, map) == "recheck 12.345 mm and P/N ABC-123");
    // Inbound rehydrate restores the real values (round-trip through the wire shape).
    std::string inbound = scrubbed;  // model echoes the scrubbed text back
    CHECK(redaction_rehydrate_text(inbound, map) == src);
    CHECK(redaction_rehydrate_text("set " + scrubbed, map) == "set " + src);
    // Unknown placeholder tokens pass through untouched.
    CHECK(redaction_rehydrate_text("note DIM_999 here", map) == "note DIM_999 here");
}

TEST_CASE("redaction gate: mode=off is a no-op, never wraps") {
    CHECK_FALSE(redaction_active("anthropic", json::object()));
    CHECK_FALSE(redaction_active("anthropic", json{{"compliance_mode", "off"}}));
    CHECK_FALSE(redaction_active("openai", json{{"compliance_mode", "off"},
                                                {"base_url", "https://api.openai.com/v1"}}));
    // And with no map allocated, scrub/rehydrate on an empty map are identity.
    RedactionMap map;
    CHECK(redaction_rehydrate_text("plain text 12.345 mm", map) == "plain text 12.345 mm");
}

TEST_CASE("redaction gate: loopback providers are never wrapped in redact mode") {
    json redact = json{{"compliance_mode", "redact"}};
    // Cloud (non-loopback) providers ARE wrapped.
    CHECK(redaction_active("anthropic", redact));
    CHECK(redaction_active("anthropic-sub", redact));
    CHECK(redaction_active("gemini", redact));
    CHECK(redaction_active("xai", redact));
    CHECK(redaction_active("moonshot", redact));
    json cloud_oai = redact;
    cloud_oai["base_url"] = "https://api.openai.com/v1";
    CHECK(redaction_active("openai", cloud_oai));
    // Loopback/local providers are NEVER wrapped, even in redact mode.
    CHECK_FALSE(redaction_active("mock", redact));
    CHECK_FALSE(redaction_active("llamacpp", redact));
    json local_oai = redact;
    local_oai["base_url"] = "http://localhost:1234/v1";
    CHECK_FALSE(redaction_active("openai", local_oai));
    json flagged = redact;
    flagged["offline_safe"] = true;
    CHECK_FALSE(redaction_active("custom-local", flagged));
}

TEST_CASE("redaction scrub_messages blocks images outbound") {
    RedactionMap map;
    json messages = json::array({user_message(json::array(
        {text_part("bore 12.345 mm, see screenshot"), image_part("/tmp/shot.png")}))});
    json out = redaction_scrub_messages(messages, map);
    REQUIRE(out.is_array());
    REQUIRE(out.size() == 1);
    json content = out[0]["content"];
    REQUIRE(content.is_array());
    REQUIRE(content.size() == 2);
    // Text part scrubbed.
    CHECK(content[0].value("type", std::string()) == "text");
    CHECK(content[0].value("text", std::string()).find("12.345") == std::string::npos);
    // Image part dropped and replaced with the compliance marker.
    CHECK(content[1].value("type", std::string()) == "text");
    CHECK(content[1].value("text", std::string()) ==
          "[image redacted — compliance_mode=redact blocks outbound images]");
    CHECK(content[1].value("text", std::string()).find("/tmp/shot.png") == std::string::npos);
}

TEST_CASE("compliance_mode=local_only refuses non-loopback, redact threads descriptor") {
    // local_only reuses the offline machinery: cloud refused before any network use.
    json refused = resolve_provider(std::string("anthropic"),
                                    json{{"compliance_mode", "local_only"},
                                         {"api_key", "sk-test"}},
                                    json::object());
    REQUIRE(refused.is_object());
    CHECK(refused.contains("error"));
    // A loopback openai endpoint stays allowed under local_only.
    json local = resolve_provider(std::string("openai"),
                                  json{{"compliance_mode", "local_only"},
                                       {"openai", {{"base_url", "http://localhost:1234/v1"}}}},
                                  json::object());
    REQUIRE(local.is_object());
    CHECK_FALSE(local.contains("error"));
    CHECK(local.value("offline_safe", false) == true);
    // redact mode does NOT refuse cloud — it threads the mode so complete() wraps traffic.
    json redact = resolve_provider(std::string("anthropic"),
                                   json{{"compliance_mode", "redact"},
                                        {"api_key", "sk-test"}},
                                   json::object());
    REQUIRE(redact.is_object());
    CHECK_FALSE(redact.contains("error"));
    CHECK(redact.value("compliance_mode", std::string()) == "redact");
    CHECK(redact.value("offline_safe", true) == false);
}

TEST_CASE("assist prompt retains numeric form values and document context") {
    const ojson params = {
        {"intent", "Set the enclosure width"},
        {"form_schema", {{"fields", ojson::array({{{"key", "width"}, {"kind", "dim"}}})}}},
        {"current", {{"fields", {{"width", {{"value", 12.5}}}}}}},
        {"selected_refs", ojson::array()}
    };
    const auto prompt = build_pm_assist_prompt(params, nullptr, "doc", 3, ojson::array());
    const auto body = ojson::parse(prompt.body);
    CHECK(body["intent"] == "Set the enclosure width");
    CHECK(body["current"]["fields"]["width"]["value"].get<double>() == doctest::Approx(12.5));
    CHECK(ask_param_summary(ojson{{"width", 12.5}}) == "width=12.5");
}
