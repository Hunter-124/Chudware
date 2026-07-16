// test_agent.cpp — unit tests for the agent provider layer (no network).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "agent/providers.h"
#include "agent/config.h"
#include "agent/state.h"

using namespace aicad::agent;

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

    // Default / adaptive (opus-4-8): thinking adaptive + effort (default high).
    {
        BuiltRequest r = anthropic_build_request("claude-opus-4-8", "sys", messages, 8000);
        CHECK(r.url == "https://api.anthropic.com/v1/messages");
        CHECK(r.body_json["model"] == "claude-opus-4-8");
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

TEST_CASE("context_window_for: override wins, else per-provider default") {
    CHECK(context_window_for(json{{"name", "gemini"}}) == 1048576);
    CHECK(context_window_for(json{{"name", "anthropic"}}) == 200000);
    CHECK(context_window_for(json{{"name", "openai"}}) == 128000);
    CHECK(context_window_for(json{{"name", "local"}}) == 8192);   // conservative default
    CHECK(context_window_for(json::object()) == 8192);            // unknown -> conservative
    // Explicit override beats the default; 0/negative is ignored.
    CHECK(context_window_for(json{{"name", "local"}, {"context_window", 16384}}) == 16384);
    CHECK(context_window_for(json{{"name", "gemini"}, {"context_window", 0}}) == 1048576);
}

TEST_CASE("fit_call_budget: input + output never overrun the window") {
    // Small LOCAL window: BOTH input cap and output must shrink so they fit with margin. This is
    // the case that used to break the chain (8192 out + ~2400 in > 8k -> server-truncated prompt).
    {
        json local{{"name", "local"}};  // window 8192
        CallBudget b = fit_call_budget(local, 8192);
        CHECK(b.output > 0);
        CHECK(b.input_cap >= 1024);                 // request/step always has room
        CHECK(b.input_cap + b.output <= 8192);      // never exceeds the window
        CHECK(b.output < 8192);                     // the 8192 reasoning ask was clamped down
    }
    // Large CLOUD window: output stays at the desired ask, input cap stays lean (kContextTokenCap).
    {
        json gemini{{"name", "gemini"}};  // window 1048576
        CallBudget b = fit_call_budget(gemini, 12288);
        CHECK(b.output == 12288);
        CHECK(b.input_cap == kContextTokenCap);
    }
    // Explicit tiny window still produces a usable, non-overrunning split.
    {
        json tiny{{"name", "local"}, {"context_window", 4096}};
        CallBudget b = fit_call_budget(tiny, 8192);
        CHECK(b.input_cap + b.output <= 4096);
        CHECK(b.input_cap >= 1024);
        CHECK(b.output >= 1024);
    }
}

TEST_CASE("apply_experimental: absent block is a no-op (parity-safe)") {
    json main = json{{"name", "gemini"}, {"model", "gemini-2.5-flash"}, {"supports_vision", true}};
    json out = apply_experimental(main, json::object(), json::object());
    CHECK(out == main);  // byte-identical when no experimental config
    CHECK_FALSE(out.contains("review_provider"));
    CHECK_FALSE(out.contains("cad_sequence_mode"));
}

TEST_CASE("resolve_review_provider: local vision endpoint resolves, vision forced on") {
    json cfg = json{
        {"experimental",
         {{"vision_review",
           {{"enabled", true},
            {"base_url", "http://localhost:1234/v1"},
            {"model", "qwen2.5-vl-7b-instruct"}}}}}};
    json rp = resolve_review_provider(cfg, json::object());
    REQUIRE(rp.is_object());
    CHECK_FALSE(rp.contains("error"));
    CHECK(rp.value("name", std::string()) == "openai");
    CHECK(rp.value("model", std::string()) == "qwen2.5-vl-7b-instruct");
    CHECK(rp.value("supports_vision", false) == true);
    CHECK(rp.value("offline_safe", false) == true);  // loopback => offline-safe

    // apply_experimental attaches it onto the main descriptor.
    json main = json{{"name", "gemini"}, {"supports_vision", true}};
    json out = apply_experimental(main, cfg, json::object());
    REQUIRE(out.contains("review_provider"));
    CHECK(out["review_provider"].value("model", std::string()) == "qwen2.5-vl-7b-instruct");
}

TEST_CASE("resolve_review_provider: disabled or absent yields null") {
    CHECK(resolve_review_provider(json::object(), json::object()).is_null());
    json off = json{{"experimental", {{"vision_review", {{"enabled", false}}}}}};
    CHECK(resolve_review_provider(off, json::object()).is_null());
}

TEST_CASE("apply_experimental: offline refuses a cloud review endpoint, falls back silently") {
    // Offline + a non-loopback review base_url => resolve_review_provider returns {error}; the
    // main (local) provider must be left WITHOUT a review_provider rather than leaking egress.
    json cfg = json{
        {"offline_mode", true},
        {"experimental",
         {{"vision_review",
           {{"enabled", true}, {"base_url", "https://api.example.com/v1"}, {"model", "x"}}}}}};
    json rp = resolve_review_provider(cfg, json::object());
    REQUIRE(rp.is_object());
    CHECK(rp.contains("error"));  // refused before any network use
    json main = json{{"name", "openai"}, {"base_url", "http://localhost:1234/v1"},
                     {"supports_vision", true}};
    json out = apply_experimental(main, cfg, json::object());
    CHECK_FALSE(out.contains("review_provider"));  // skipped -> review uses the safe main provider
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

TEST_CASE("apply_experimental: cad_sequence_mode flag flows through") {
    json cfg = json{{"experimental", {{"cad_sequence_mode", true}}}};
    json out = apply_experimental(json{{"name", "openai"}}, cfg, json::object());
    CHECK(out.value("cad_sequence_mode", false) == true);
}

TEST_CASE("build_context honors a small cap and reports trimming") {
    DesignState st;
    st.request = std::string(20000, 'x');  // an oversized request that must be trimmed
    // A tiny cap forces truncation regardless of the (empty, in-test) system prompt.
    BuiltContext bc = build_context(st, "intake", "", 1024);
    CHECK(bc.context_tokens_est <= 1024);
    CHECK(bc.trimmed);
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
    // off/none disables thinking and must not inflate the cap.
    auto [cap_off, think_off] = out_cap("off", 8192);
    CHECK(think_off == 0);
    CHECK(cap_off == 8192);
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

    // Reasoning model that emitted ONLY reasoning (empty content) -> fall back to it so any
    // inline ops JSON survives instead of the run seeing an empty reply.
    json only_reason = {{"choices", json::array({{{"message",
                        {{"content", ""}, {"reasoning_content", "{\"ops\":[]}"}}}}})}};
    CHECK(openai_parse_response(only_reason).text == "{\"ops\":[]}");

    // The `reasoning` spelling (some servers) is also honored.
    json alt = {{"choices", json::array({{{"message", {{"reasoning", "r"}}}}})}};
    CHECK(openai_parse_response(alt).text == "r");
}

TEST_CASE("mock provider repeats last") {
    MockProvider mp({"a", "b"});
    CHECK(mp.complete() == "a");
    CHECK(mp.complete() == "b");
    CHECK(mp.complete() == "b");
}

TEST_CASE("role sniffing") {
    CHECK(RoleScriptedProvider::role_of("ROLE: planner\nrest") == "planner");
    CHECK(RoleScriptedProvider::role_of("You are the modeler...") == "modeler");
    CHECK(RoleScriptedProvider::role_of("...planning a CAD build...") == "planner");
    CHECK(RoleScriptedProvider::role_of("nope") == "?");
}

// T-F4: clarify() interaction/1 shape-mirror tests.
// The existing {ready, questions, assumptions} output must be preserved (additive only).
// The new "interaction" field must conform to aicad.interaction/1 (contracts/interaction.md).
#include "agent/clarify.h"

TEST_CASE("clarify: interaction field present and schema correct") {
    // clarify with a question-producing reply
    auto complete = [](const std::string&, const std::string&) -> std::string {
        return R"(```json
{"ready": false, "questions": [
  {"key": "shaft_dia", "ask": "Shaft diameter?", "unit": "mm", "suggested": "8"}
], "assumptions": []}
```)";
    };
    ojson result = clarify("make a shaft", ojson(nullptr), "doc1", 1, ojson::array(), complete);

    // Legacy fields must be preserved (additive contract).
    CHECK(result.contains("ready"));
    CHECK(result.contains("questions"));
    CHECK(result.contains("assumptions"));
    CHECK(result["ready"] == false);
    CHECK(result["questions"].size() == 1);
    // Legacy: ask field holds the question text; key is the short id.
    CHECK(result["questions"][0]["ask"] == "Shaft diameter?");
    CHECK(result["questions"][0]["key"] == "shaft_dia");

    // New T-F4 interaction field.
    CHECK(result.contains("interaction"));
    const auto& ia = result["interaction"];
    CHECK(ia["schema"] == "aicad.interaction/1");
    CHECK(ia["blocking"] == false);
    CHECK(ia["scope"] == "run");
    CHECK(ia.contains("questions"));
    CHECK(ia["questions"].size() == 1);
    const auto& iq = ia["questions"][0];
    CHECK(iq["id"] == "shaft_dia");     // stable ledger key = the `key` field
    CHECK(iq["kind"] == "dimension");   // non-empty unit → dimension
    CHECK(iq["prompt"] == "Shaft diameter?");
    CHECK(iq["required"] == false);
    CHECK(iq["family"] == "length");
    CHECK(iq["suggested"]["unit"] == "mm");
    CHECK(iq["suggested"]["value"] == doctest::Approx(8.0));
}

TEST_CASE("clarify: interaction field on ready=true (no questions)") {
    auto complete = [](const std::string&, const std::string&) -> std::string {
        return R"({"ready": true, "questions": [], "assumptions": ["assume M3"]})";
    };
    ojson result = clarify("make M3 standoff", ojson(nullptr), "d", 1, ojson::array(), complete);
    CHECK(result["ready"] == true);
    CHECK(result["questions"].size() == 0);
    // interaction still present, but with empty questions array.
    CHECK(result.contains("interaction"));
    CHECK(result["interaction"]["questions"].size() == 0);
}

TEST_CASE("clarify: text-kind question when unit is empty") {
    auto complete = [](const std::string&, const std::string&) -> std::string {
        return R"({"ready": false, "questions": [
          {"key": "finish", "ask": "Surface finish?", "unit": "", "suggested": "as_machined"}
        ], "assumptions": []})";
    };
    ojson result = clarify("make part", ojson(nullptr), "d", 1, ojson::array(), complete);
    const auto& iq = result["interaction"]["questions"][0];
    CHECK(iq["kind"] == "text");
    CHECK(iq["suggested"] == "as_machined");
}

TEST_CASE("clarify: interaction fields additive — existing callers unaffected") {
    // Verifies that adding 'interaction' to the output does not remove or rename
    // any existing field (safe for drawClarifyCard / ClarifyState consumers).
    auto complete = [](const std::string&, const std::string&) -> std::string {
        return R"({"ready": false, "questions": [
          {"key": "bore", "ask": "Bore diameter?", "unit": "mm", "suggested": "10"},
          {"key": "finish", "ask": "Surface finish?", "unit": "", "suggested": "anodized"}
        ], "assumptions": ["standard tolerance"]})";
    };
    ojson result = clarify("make housing", ojson(nullptr), "d", 1, ojson::array(), complete);

    // Legacy shape (unchanged).
    CHECK(result["ready"] == false);
    CHECK(result["questions"].size() == 2);
    CHECK(result["questions"][0]["key"] == "bore");
    CHECK(result["questions"][0]["ask"] == "Bore diameter?");
    CHECK(result["questions"][0]["unit"] == "mm");
    CHECK(result["questions"][0]["suggested"] == "10");
    CHECK(result["assumptions"].size() == 1);
    CHECK(result["assumptions"][0] == "standard tolerance");

    // Additive: interaction present alongside legacy.
    CHECK(result.contains("interaction"));
    CHECK(result["interaction"]["questions"].size() == 2);
}
