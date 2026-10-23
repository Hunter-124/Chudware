// test_agent.cpp — unit tests for the agent provider layer (no network).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "agent/providers.h"
#include "agent/config.h"
#include "agent/machine.h"
#include "agent/state.h"
#include "agent/roles.h"
#include "agent/events.h"
#include "agent/ops_emit.h"
#include "agent/complexity.h"
#include "agent/domain.h"
#include "agent/firm_bid.h"
#include "agent/iface_plan.h"
#include "agent/run.h"
#include "agent/mission.h"


using namespace chudware::agent;


TEST_CASE("sanitize_part_name keeps revision identity in PLM") {
    CHECK(sanitize_part_name("holster_v1") == "holster");
    CHECK(sanitize_part_name("bracket-rev2") == "bracket");
    CHECK(sanitize_part_name("arm gen3") == "arm");
    CHECK(sanitize_part_name("sixaxis_arm_revD_final5") == "sixaxis_arm");
    CHECK(sanitize_part_name("base_revD") == "base");
    CHECK(sanitize_part_name("arm-rev C") == "arm");
    CHECK(sanitize_part_name("part_final2") == "part");
    CHECK(sanitize_part_name("PN-1234") == "PN-1234");
    CHECK(sanitize_part_name("v1") == "v1");

    // Version stripping happens before the existing illegal-character removal and length cap.
    CHECK(sanitize_part_name(
              "Very/Long:Part*Name?With<Illegal>|Characters_and_more_characters_rev12") ==
          "VeryLongPartNameWithIllegalCharacters_and_more_c");
}

TEST_CASE("mission mint boundary normalizes model-authored names and paths") {
    CHECK(mission_mint_name("gear_rev2", "Part") == "gear");
    CHECK(mission_mint_name("Motor/Gear", "Part") == "MotorGear");
    CHECK(mission_mint_name("../outside_rev3", "Part") == "..outside");
    CHECK(mission_mint_name("", "Assembly") == "Assembly");

    const auto root =
        std::filesystem::temp_directory_path() / "chudware_test_agent_mission_names";
    const auto output = mission_output_path(root, "../outside_rev3", "Assembly");
    CHECK(output.filename() == "..outside.step+");
    CHECK(mission_path_is_beneath(root, output));
    CHECK(output.parent_path() == std::filesystem::absolute(root).lexically_normal());
    CHECK(mission_output_path(root, "", "Assembly").filename() == "Assembly.step+");
}

TEST_CASE("worker workspace emission is absent or an existing directory") {
    const auto root =
        std::filesystem::temp_directory_path() / "chudware_test_agent_workspace_emission";
    const auto missing = root / "missing";
    std::error_code ec;
    std::filesystem::remove_all(root, ec);

    json params = json::object();
    CHECK_FALSE(emit_existing_workspace_dir(params, missing.string()));
    CHECK_FALSE(params.contains("workspace_dir"));

    std::filesystem::create_directories(root, ec);
    REQUIRE_FALSE(ec);
    CHECK(emit_existing_workspace_dir(params, root.string()));
    REQUIRE(params.contains("workspace_dir"));
    CHECK(std::filesystem::is_directory(params["workspace_dir"].get<std::string>()));

    std::filesystem::remove_all(root, ec);
}

TEST_CASE("assembly child acceptance requires success and proven geometry") {
    const auto success =
        classify_assembly_child(json{{"status", "done"}, {"stop_reason", "built"}}, true);
    CHECK(success.accept);
    CHECK_FALSE(success.cancelled);
    CHECK(success.failure_reason.empty());

    const auto empty =
        classify_assembly_child(json{{"status", "done"}, {"stop_reason", "built"}}, false);
    CHECK_FALSE(empty.accept);
    CHECK(empty.failure_reason == "no_geometry");

    const auto failed = classify_assembly_child(
        json{{"status", "failed"}, {"stop_reason", "provider_error"}}, true);
    CHECK_FALSE(failed.accept);
    CHECK(failed.failure_reason == "provider_error");

    const auto cancelled = classify_assembly_child(
        json{{"status", "cancelled"}, {"stop_reason", "cancelled"}}, true);
    CHECK_FALSE(cancelled.accept);
    CHECK(cancelled.cancelled);

    const auto mixed = classify_assembly_finish(1, true, "provider_error");
    CHECK(mixed.status == "partial");
    CHECK(mixed.stop_reason == "provider_error");
    CHECK(classify_assembly_finish(2, false, "").status == "done");
    CHECK(classify_assembly_finish(0, true, "no_geometry").status == "failed");
}

TEST_CASE("mission rejects planner capabilities without native executors") {
    CHECK(mission_capability_has_executor("design"));
    CHECK(mission_capability_has_executor("assembly"));
    CHECK(mission_capability_has_executor("simulate_flow"));
    CHECK(mission_capability_has_executor("report"));
    CHECK(mission_capability_has_executor("reverse_engineer"));

    CHECK_FALSE(mission_capability_has_executor("simulate_fea"));
    CHECK_FALSE(mission_capability_has_executor("drawing"));
    CHECK_FALSE(mission_capability_has_executor("evaluate"));

    const auto unavailable = classify_mission_capability("simulate_fea");
    CHECK_FALSE(unavailable.executable);
    CHECK(unavailable.stop_reason == "capability_unavailable");
    CHECK(classify_mission_capability("design").executable);
    CHECK_FALSE(mission_capability_has_executor("unknown_future_capability"));
}
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
    // The key is present on purpose so the refusal proves the OFFLINE guard, not the
    // missing-credential guard below.
    json cfg = json{
        {"offline_mode", true},
        {"experimental",
         {{"vision_review",
           {{"enabled", true},
            {"base_url", "https://api.example.com/v1"},
            {"api_key", "sk-test"},
            {"model", "x"}}}}}};
    json rp = resolve_review_provider(cfg, json::object());
    REQUIRE(rp.is_object());
    CHECK(rp.contains("error"));  // refused before any network use
    json main = json{{"name", "openai"}, {"base_url", "http://localhost:1234/v1"},
                     {"supports_vision", true}};
    json out = apply_experimental(main, cfg, json::object());
    CHECK_FALSE(out.contains("review_provider"));  // skipped -> review uses the safe main provider
}

TEST_CASE("resolve_review_provider: cloud endpoint with no key refuses instead of 401ing") {
    // A remote vision endpoint with no resolvable credential used to be handed the literal
    // "lm-studio" placeholder, which the service rejected as HTTP 401 -> stop_reason
    // provider_auth. Refuse here so apply_experimental skips the split instead.
    json cfg = json{
        {"experimental",
         {{"vision_review",
           {{"enabled", true},
            {"base_url", "https://api.moonshot.ai/v1"},
            {"model", "kimi-k3"}}}}}};
    json rp = resolve_review_provider(cfg, json::object());
    REQUIRE(rp.is_object());
    CHECK(rp.contains("error"));
    json main = json{{"name", "gemini"}, {"supports_vision", true}};
    CHECK_FALSE(apply_experimental(main, cfg, json::object()).contains("review_provider"));
}

TEST_CASE("resolve_review_provider: moonshot endpoint routes native and borrows keys.moonshot") {
    // The Settings row carries no key of its own; the Kimi credential lives in keys.moonshot
    // (NOT keys.cloud_compat), and the endpoint must resolve as the first-class "moonshot"
    // provider so the wire-id aliases and the kimi reasoning ladder apply.
    json cfg = json{
        {"keys", {{"moonshot", "sk-moon"}, {"cloud_compat", ""}}},
        {"experimental",
         {{"vision_review",
           {{"enabled", true},
            {"base_url", "https://api.moonshot.ai/v1"},
            {"model", "kimi-k3"}}}}}};
    json rp = resolve_review_provider(cfg, json::object());
    REQUIRE(rp.is_object());
    CHECK_FALSE(rp.contains("error"));
    CHECK(rp.value("name", std::string()) == "moonshot");
    CHECK(rp.value("api_key", std::string()) == "sk-moon");
    CHECK(rp.value("model", std::string()) == "kimi-k3");
    CHECK(rp.value("supports_vision", false) == true);
    CHECK(rp.value("offline_safe", true) == false);  // cloud
}

TEST_CASE("resolve_review_provider: generic cloud endpoint borrows keys.cloud_compat") {
    json cfg = json{
        {"keys", {{"cloud_compat", "sk-cloud"}}},
        {"experimental",
         {{"vision_review",
           {{"enabled", true},
            {"base_url", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"},
            {"model", "qwen3-vl-plus"}}}}}};
    json rp = resolve_review_provider(cfg, json::object());
    REQUIRE(rp.is_object());
    CHECK_FALSE(rp.contains("error"));
    CHECK(rp.value("name", std::string()) == "openai");
    CHECK(rp.value("api_key", std::string()) == "sk-cloud");
}

TEST_CASE("resolve_review_provider: an explicit row key wins over the keys.* fallback") {
    json cfg = json{
        {"keys", {{"moonshot", "sk-moon"}}},
        {"experimental",
         {{"vision_review",
           {{"enabled", true},
            {"base_url", "https://api.moonshot.ai/v1"},
            {"api_key", "sk-explicit"},
            {"model", "kimi-k3"}}}}}};
    json rp = resolve_review_provider(cfg, json::object());
    REQUIRE(rp.is_object());
    CHECK(rp.value("api_key", std::string()) == "sk-explicit");
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

TEST_CASE("load_eng_firm_models: empty config yields empty entries") {
    EngFirmModels d = load_eng_firm_models(json::object());
    CHECK(d.mission_planner.empty());
    CHECK(d.advisor.empty());
    CHECK(d.lead.empty());
    CHECK(d.subcontractor.empty());
    CHECK(d.disciplines.empty());
    CHECK(resolve_eng_firm_provider(d.mission_planner, json::object()).is_null());
    CHECK(resolve_eng_firm_provider(d.advisor, json::object()).is_null());
    CHECK(resolve_eng_firm_provider(d.lead, json::object()).is_null());
    CHECK(resolve_eng_firm_provider(d.subcontractor, json::object()).is_null());
}

TEST_CASE("resolve_eng_firm_provider: role entries resolve provider+model+thinking") {
    json cfg = json{
        {"eng_firm",
         {{"models",
           {{"mission_planner",
             {{"provider", "moonshot"}, {"model", "kimi-k3"}, {"thinking", "high"}}},
            {"advisor", {{"provider", "openai"}, {"model", "gpt-4.1-mini"}, {"thinking", "low"}}},
            {"lead", {{"provider", "openai"}, {"model", "gpt-4.1"}, {"thinking", "medium"}}},
            {"subcontractor",
             {{"provider", "openai"}, {"model", "gpt-4.1-nano"}, {"thinking", "minimal"}}}}}}},
        {"api_key", "sk-test"},
        {"keys", {{"moonshot", "ms-test"}}},
        {"openai", {{"base_url", "http://localhost:1234/v1"}}},
        {"moonshot", {{"base_url", "http://localhost:1234/v1"}}}};

    EngFirmModels m = load_eng_firm_models(cfg);
    CHECK_FALSE(m.mission_planner.empty());
    CHECK(m.mission_planner.provider == "moonshot");
    CHECK(m.mission_planner.model == "kimi-k3");
    CHECK(m.mission_planner.thinking == "high");

    json mp = resolve_eng_firm_provider(m.mission_planner, cfg, json::object());
    REQUIRE(mp.is_object());
    CHECK_FALSE(mp.contains("error"));
    CHECK(mp.value("name", std::string()) == "moonshot");
    CHECK(mp.value("model", std::string()) == "kimi-k3");
    CHECK(mp.value("reasoning_effort", std::string()) == "high");

    json adv = resolve_eng_firm_provider(m.advisor, cfg, json::object());
    REQUIRE(adv.is_object());
    CHECK(adv.value("name", std::string()) == "openai");
    CHECK(adv.value("model", std::string()) == "gpt-4.1-mini");
    CHECK(adv.value("reasoning_effort", std::string()) == "low");

    json lead = resolve_eng_firm_provider(m.lead, cfg, json::object());
    REQUIRE(lead.is_object());
    CHECK(lead.value("name", std::string()) == "openai");
    CHECK(lead.value("model", std::string()) == "gpt-4.1");

    json sub = resolve_eng_firm_provider(m.subcontractor, cfg, json::object());
    REQUIRE(sub.is_object());
    CHECK(sub.value("name", std::string()) == "openai");
    CHECK(sub.value("model", std::string()) == "gpt-4.1-nano");
}

TEST_CASE("eng_firm models: discipline beats lead; subcontractor default; malformed safe") {
    // disciplines.mechanical beats lead for a mechanical task domain.
    json cfg = json{
        {"eng_firm",
         {{"models",
           {{"lead", {{"provider", "openai"}, {"model", "lead-model"}}},
            {"subcontractor", {{"provider", "openai"}, {"model", "sub-model"}}},
            {"disciplines",
             {{"mechanical", {{"provider", "openai"}, {"model", "mech-model"}}}}}}}}},
        {"api_key", "sk-test"},
        {"openai", {{"base_url", "http://localhost:1234/v1"}}}};

    EngFirmModels m = load_eng_firm_models(cfg);
    CHECK(eng_firm_discipline_key(Discipline::Mechanical) == "mechanical");
    CHECK(eng_firm_discipline_key(Discipline::Generalist) == "generalist");

    // Mechanical discipline entry wins over lead.
    auto dit = m.disciplines.find("mechanical");
    REQUIRE(dit != m.disciplines.end());
    json mech = resolve_eng_firm_provider(dit->second, cfg, json::object());
    REQUIRE(mech.is_object());
    CHECK(mech.value("model", std::string()) == "mech-model");

    // No electrical discipline → subcontractor default applies for that role.
    CHECK(m.disciplines.count("electrical") == 0);
    json sub = resolve_eng_firm_provider(m.subcontractor, cfg, json::object());
    REQUIRE(sub.is_object());
    CHECK(sub.value("model", std::string()) == "sub-model");

    // lead still resolves when used as fallback.
    json lead = resolve_eng_firm_provider(m.lead, cfg, json::object());
    REQUIRE(lead.is_object());
    CHECK(lead.value("model", std::string()) == "lead-model");

    // Malformed entries never throw: wrong types → empty / null.
    json bad = json{{"eng_firm",
                     {{"models",
                       {{"mission_planner", "not-an-object"},
                        {"advisor", 42},
                        {"lead", nullptr},
                        {"subcontractor", json::array()},
                        {"disciplines", "nope"}}}}},
                    {"api_key", "sk-test"}};
    CHECK_NOTHROW({
        EngFirmModels bm = load_eng_firm_models(bad);
        CHECK(bm.mission_planner.empty());
        CHECK(bm.advisor.empty());
        CHECK(bm.lead.empty());
        CHECK(bm.subcontractor.empty());
        CHECK(bm.disciplines.empty());
        CHECK(resolve_eng_firm_provider(bm.mission_planner, bad, json::object()).is_null());
        // Non-object eng_firm / models also safe.
        CHECK(load_eng_firm_models(json{{"eng_firm", 1}}).mission_planner.empty());
        CHECK(load_eng_firm_models(json{{"eng_firm", {{"models", 1}}}}).advisor.empty());
        // Empty entry resolves null.
        EngFirmModelEntry empty;
        CHECK(resolve_eng_firm_provider(empty, cfg, json::object()).is_null());
    });
}

TEST_CASE("is_implementation_stage / is_planning_stage classify prewalk switch") {
    CHECK(is_planning_stage("intake"));
    CHECK(is_planning_stage("plan"));
    CHECK(is_planning_stage("review"));
    CHECK_FALSE(is_planning_stage("model"));
    CHECK(is_implementation_stage("model"));
    CHECK(is_implementation_stage("fix"));
    CHECK(is_implementation_stage("execute"));
    CHECK_FALSE(is_implementation_stage("plan"));
}

TEST_CASE("build_context honors a small cap and reports trimming") {
    DesignState st;
    st.request = std::string(20000, 'x');  // an oversized request that must be trimmed
    // A tiny cap forces truncation regardless of the (empty, in-test) system prompt.
    BuiltContext bc = build_context(st, "intake", "", 1024);
    CHECK(bc.context_tokens_est <= 1024);
    CHECK(bc.trimmed);
}

TEST_CASE("build_context intake includes open-doc feature digest") {
    DesignState st;
    st.request = "chamfer the top edges 0.5mm";
    st.revision = 3;
    st.tree_digest = "f1 extrude Washer\nf2 cut Bore";
    BuiltContext bc = build_context(st, "intake", "", kContextTokenCap);
    // Intake must see existing geometry so edit follow-ups are not rejected as object-less.
    CHECK(bc.body.find("Context: the open part already has features:") != std::string::npos);
    CHECK(bc.body.find("f1 extrude Washer") != std::string::npos);
    CHECK(bc.body.find("(revision 3)") != std::string::npos);
    CHECK(bc.body.find("chamfer the top edges 0.5mm") != std::string::npos);
}

TEST_CASE("build_context intake omits doc context when empty") {
    DesignState st;
    st.request = "make a 20mm cube";
    st.revision = 0;
    st.tree_digest = "(empty — no features yet)";
    BuiltContext bc = build_context(st, "intake", "", kContextTokenCap);
    CHECK(bc.body.find("Context: the open part already has features:") == std::string::npos);
    CHECK(bc.body.find("make a 20mm cube") != std::string::npos);
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

TEST_CASE("role sniffing") {
    CHECK(RoleScriptedProvider::role_of("ROLE: planner\nrest") == "planner");
    CHECK(RoleScriptedProvider::role_of("You are the modeler...") == "modeler");
    CHECK(RoleScriptedProvider::role_of("...planning a CAD build...") == "planner");
    CHECK(RoleScriptedProvider::role_of("nope") == "?");
}

// clarify() interaction/1 shape-mirror tests.
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

    // New interaction field.
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

TEST_CASE("clarify: choice kind with options mirrors multi and single") {
    auto complete = [](const std::string&, const std::string&) -> std::string {
        return R"({"ready": false, "questions": [
          {"key": "mat", "ask": "Material?", "unit": "", "suggested": "aluminum",
           "kind": "choice",
           "options": [{"value":"aluminum","label":"6061"},{"value":"steel","label":"1018"}]},
          {"key": "keep", "ask": "Keep which?", "unit": "",
           "kind": "choice", "multi": true,
           "options": [{"value":"rib"},{"value":"fillet"}],
           "suggested": ["rib","fillet"]}
        ], "assumptions": []})";
    };
    ojson result = clarify("bracket", ojson(nullptr), "d", 1, ojson::array(), complete);
    CHECK(result["interaction"]["questions"].size() == 2);
    const auto& mat = result["interaction"]["questions"][0];
    CHECK(mat["kind"] == "choice");
    CHECK(mat["multi"] == false);
    CHECK(mat["options"].size() == 2);
    CHECK(mat["suggested"] == "aluminum");
    const auto& keep = result["interaction"]["questions"][1];
    CHECK(keep["kind"] == "choice");
    CHECK(keep["multi"] == true);
    CHECK(keep["suggested"].is_array());
    CHECK(keep["suggested"].size() == 2);
}

TEST_CASE("clarify: geometry_select kind emits empty suggested + targets") {
    auto complete = [](const std::string&, const std::string&) -> std::string {
        return R"({"ready": false, "questions": [
          {"key": "edit_face", "ask": "Which face for the pocket?",
           "kind": "geometry_select", "targets": ["face"], "suggested": ""}
        ], "assumptions": []})";
    };
    ojson result = clarify("pocket the top", ojson(nullptr), "d", 1, ojson::array(), complete);
    const auto& iq = result["interaction"]["questions"][0];
    CHECK(iq["kind"] == "geometry_select");
    CHECK(iq["targets"][0] == "face");
    CHECK(iq["suggested"].is_object());
    CHECK(iq["suggested"].empty());
    CHECK(iq["required"] == true);
    // Legacy list still has key/ask for old consumers.
    CHECK(result["questions"][0]["key"] == "edit_face");
    CHECK(result["questions"][0]["kind"] == "geometry_select");
}

// --------------------------------------------------------------------------- tool_call target (humanize)
TEST_CASE("emit_agent_tool_call demotes opaque doc_id; prefers name and ops") {
    std::vector<json> seen;
    RunEvents ev("target-humanize", [&](const json& e) { seen.push_back(e); });

    // new_part-style: name beats doc_id on the same object.
    seen.clear();
    emit_agent_tool_call(ev, "new_part", /*target=*/"",
                         json{{"name", "mud_bucket"}, {"doc_id", "doc-cpp00000002"}},
                         true, 1);
    REQUIRE(!seen.empty());
    CHECK(seen.back()["target"] == "mud_bucket");

    // MCP add_features envelope: ops feature summary beats opaque doc_id.
    seen.clear();
    emit_agent_tool_call(
        ev, "add_features", /*target=*/"",
        json{{"doc_id", "doc-cpp00000009"},
             {"ops",
              json::array({json{{"op", "add_feature"},
                                {"feature",
                                 {{"type", "extrude"},
                                  {"name", "BucketWall"},
                                  {"params", {{"depth", 40}}}}}}})}},
        true, 1);
    REQUIRE(!seen.empty());
    CHECK(seen.back()["target"].get<std::string>().find("BucketWall") != std::string::npos);
    CHECK(seen.back()["target"].get<std::string>().find("doc-cpp") == std::string::npos);

    // Bare opaque doc_id alone must not become the human target.
    seen.clear();
    emit_agent_tool_call(ev, "add_features", /*target=*/"",
                         json{{"doc_id", "doc-cpp00000001"}}, true, 1);
    REQUIRE(!seen.empty());
    CHECK(seen.back()["target"].get<std::string>().find("doc-cpp") == std::string::npos);
}

// --------------------------------------------------------------------------- plan_state
TEST_CASE("plan_item_title caps at 10 words") {
    CHECK(plan_item_title("one two three") == "one two three");
    CHECK(plan_item_title("a b c d e f g h i j k l") == "a b c d e f g h i j");
    CHECK(plan_item_title("  spaced   words  here  ") == "spaced words here");
}

TEST_CASE("plan_state items from build_plan") {
    json plan = {{"name", "Hex standoff"},
                 {"summary", "extruded hex prism"},
                 {"parts",
                  json::array(
                      {json{{"name", "base"},
                            {"purpose", "main body"},
                            {"dimensions", {{"w", 20}, {"h", 10}}},
                            {"features", json::array({"fillet top edges", "bore M3"})}}})}};
    json items = plan_state_items_from_build_plan(plan);
    REQUIRE(items.size() == 1);
    CHECK(items[0]["id"] == "base");
    CHECK(items[0]["title"] == "base");
    CHECK(items[0]["kind"] == "part");
    CHECK(items[0]["status"] == "pending");
    std::string desc = items[0]["desc"].get<std::string>();
    CHECK(desc.find("main body") != std::string::npos);
    CHECK(desc.find("w=") != std::string::npos);
    CHECK(desc.find("fillet") != std::string::npos);

    std::string title, summary;
    plan_display_meta(ojson(plan), title, summary);
    CHECK(title == "Hex standoff");
    CHECK(summary == "extruded hex prism");

    json payload = make_plan_state("build_plan", title, summary, 1, items);
    CHECK(payload["shape"] == "build_plan");
    CHECK(payload["revision"] == 1);
    CHECK(payload["items"].size() == 1);
    CHECK_FALSE(payload.contains("edges"));
}

TEST_CASE("plan_state checklist step statuses") {
    std::vector<std::string> steps = {"Build base solid", "Cut the bore", "Add fillet"};
    json pending = plan_state_items_from_steps(steps, -1);
    REQUIRE(pending.size() == 3);
    CHECK(pending[0]["status"] == "pending");
    CHECK(pending[0]["kind"] == "step");
    CHECK(pending[0]["id"] == "model:0");
    CHECK(pending[0]["title"] == "Build base solid");

    json running = plan_state_items_from_steps(steps, 1);
    CHECK(running[0]["status"] == "done");
    CHECK(running[1]["status"] == "running");
    CHECK(running[2]["status"] == "pending");

    json failed = plan_state_items_from_steps(steps, -1, false, 1);
    CHECK(failed[0]["status"] == "done");
    CHECK(failed[1]["status"] == "fail");
    CHECK(failed[2]["status"] == "pending");

    json done = plan_state_items_from_steps(steps, -1, true);
    CHECK(done[0]["status"] == "done");
    CHECK(done[2]["status"] == "done");
}

TEST_CASE("plan_state objectives extraction no-op when absent") {
    json plan = {{"name", "x"}, {"parts", json::array({json{{"name", "a"}}})}};
    CHECK(plan_state_items_from_objectives(plan).empty());

    plan["objectives"] = json::array({"Fit M3 screw", "Wall thickness >= 2mm"});
    json objs = plan_state_items_from_objectives(plan);
    REQUIRE(objs.size() == 2);
    CHECK(objs[0]["kind"] == "objective");
    CHECK(objs[0]["status"] == "pending");
    CHECK(objs[0]["title"] == "Fit M3 screw");

    plan.erase("objectives");
    plan["acceptance"] = json::array({json{{"title", "Pass FEA FoS>2"}, {"desc", "static load"}}});
    objs = plan_state_items_from_objectives(plan);
    REQUIRE(objs.size() == 1);
    CHECK(objs[0]["desc"] == "static load");
}

TEST_CASE("plan_state mission task items") {
    json tasks = json::array({json{{"capability", "design"}, {"title", "Build bracket"}},
                              json{{"capability", "drawing"},
                                   {"title", "Make drawing"},
                                   {"status", "done"}}});
    json items = plan_state_items_from_mission(tasks, "Mission");
    REQUIRE(items.size() == 2);
    CHECK(items[0]["kind"] == "task");
    CHECK(items[0]["status"] == "pending");
    CHECK(items[0]["phase"] == "Mission");
    CHECK(items[0]["meta"]["capability"] == "design");
    CHECK(items[1]["status"] == "done");
}

TEST_CASE("run_machine emits plan_state build_plan and checklist") {
    DesignState st;
    st.request = "hex standoff";
    st.doc_id = "doc-test";

    StageActions actions;
    actions.intake = [](DesignState&, int) {
        StageOutcome o;
        o.verdict = "pass";
        o.reason = "ok";
        return o;
    };
    actions.plan = [](DesignState& s, int) {
        s.plan = ojson{{"name", "Standoff"},
                       {"summary", "hex body"},
                       {"parts",
                        ojson::array({ojson{{"name", "body"},
                                            {"purpose", "main"},
                                            {"dimensions", ojson{{"d", 10}}},
                                            {"features", ojson::array({"bore"})}}})},
                       {"objectives", ojson::array({"Clearance fit"})}};
        StageOutcome o;
        o.verdict = "pass";
        o.reason = "plan parsed";
        o.parsed_ok = true;
        return o;
    };
    actions.model = [](DesignState& s, int) {
        // A real model stage that passes has applied ops — advance revision and
        // stamp a non-empty tree so the zero-progress/no_geometry guard sees built
        // geometry (this test exercises plan_state events, not empty-run handling).
        s.revision += 1;
        s.tree_digest = "f1:extrude";
        StageOutcome o;
        o.verdict = "pass";
        o.reason = "modeled";
        return o;
    };
    actions.review = [](DesignState&, int) {
        StageOutcome o;
        o.verdict = "pass";
        o.reason = "ok";
        return o;
    };
    actions.fix = [](DesignState&, int) {
        StageOutcome o;
        o.verdict = "pass";
        return o;
    };
    actions.memory = [](DesignState&, int) {
        StageOutcome o;
        o.verdict = "pass";
        return o;
    };
    actions.plan_steps = [](DesignState&) {
        return std::vector<std::string>{"Build body base", "Refine body: bore"};
    };

    std::vector<json> seen;
    // Unique run id so we don't clobber other tests' event files.
    const std::string rid = "plan-state-ut";
    RunEvents events(rid, [&](const json& ev) { seen.push_back(ev); });

    json result = run_machine(st, actions, events, json{{"intake", 1},
                                                        {"plan", 1},
                                                        {"model", 1},
                                                        {"review", 1},
                                                        {"fix", 1},
                                                        {"memory", 1}});
    CHECK(result["status"] == "done");

    int n_build = 0, n_check = 0, n_obj = 0, n_plan_steps = 0, n_model_step = 0;
    json last_checklist;
    json first_checklist;
    for (const auto& ev : seen) {
        const std::string kind = ev.value("kind", std::string());
        if (kind == "plan_steps") ++n_plan_steps;
        if (kind == "model_step") ++n_model_step;
        if (kind != "plan_state") continue;
        const std::string shape = ev.value("shape", std::string());
        if (shape == "build_plan") {
            ++n_build;
            CHECK(ev["title"] == "Standoff");
            CHECK(ev["summary"] == "hex body");
            CHECK(ev["revision"] == 1);
            REQUIRE(ev["items"].is_array());
            CHECK(ev["items"].size() == 1);
            CHECK(ev["items"][0]["kind"] == "part");
            CHECK(ev["items"][0]["id"] == "body");
        } else if (shape == "checklist") {
            ++n_check;
            if (first_checklist.is_null()) first_checklist = ev;
            last_checklist = ev;
        } else if (shape == "objectives") {
            ++n_obj;
            CHECK(ev["items"].size() == 1);
            CHECK(ev["items"][0]["kind"] == "objective");
        }
    }
    CHECK(n_build == 1);
    CHECK(n_obj == 1);
    // New contract: when model-step plan_state checklist is emitted, legacy plan_steps
    // is suppressed (host used to synthesize step:N beside model:N → doubled task list).
    CHECK(n_plan_steps == 0);
    CHECK(n_model_step == 2);
    CHECK(n_check >= 2);  // initial + per-step + final
    REQUIRE(first_checklist.is_object());
    REQUIRE(first_checklist["items"].is_array());
    REQUIRE(first_checklist["items"].size() == 2);
    CHECK(first_checklist["items"][0]["id"] == "model:0");
    CHECK(first_checklist["items"][1]["id"] == "model:1");
    CHECK(first_checklist["items"][0]["kind"] == "step");
    REQUIRE(last_checklist.is_object());
    // Final checklist should mark all done.
    for (const auto& it : last_checklist["items"])
        CHECK(it["status"] == "done");
}

TEST_CASE("run_machine no plan leaves plan_state absent") {
    DesignState st;
    st.request = "noop";
    StageActions actions;
    auto pass = [](DesignState&, int) {
        StageOutcome o;
        o.verdict = "pass";
        return o;
    };
    actions.intake = pass;
    // Plan fails all attempts → st.plan stays null; machine continues (plan non-fatal).
    actions.plan = [](DesignState&, int) {
        StageOutcome o;
        o.verdict = "retry";
        o.reason = "plan not parseable";
        return o;
    };
    actions.model = pass;
    actions.review = pass;
    actions.fix = pass;
    actions.memory = pass;
    actions.plan_steps = [](DesignState&) { return std::vector<std::string>{""}; };

    std::vector<json> seen;
    RunEvents events("plan-state-noplan", [&](const json& ev) { seen.push_back(ev); });
    json budgets = {{"plan", 1}, {"model", 1}, {"review", 1}, {"fix", 1}, {"memory", 1},
                    {"intake", 1}};
    (void)run_machine(st, actions, events, budgets);

    bool has_build = false;
    bool has_checklist = false;
    for (const auto& ev : seen) {
        if (ev.value("kind", "") != "plan_state") continue;
        if (ev.value("shape", "") == "build_plan") has_build = true;
        if (ev.value("shape", "") == "checklist") has_checklist = true;
    }
    CHECK_FALSE(has_build);
    // Blank default step does not emit a plan_state checklist card (off/no-plan unchanged).
    CHECK_FALSE(has_checklist);
}

TEST_CASE("run_machine solo blank steps still emit plan_steps without model checklist") {
    // When plan is skipped (budgets.plan=0) or steps have no content, machine keeps the
    // legacy plan_steps event and does NOT emit the model:N plan_state checklist.
    DesignState st;
    st.request = "solo blank";
    StageActions actions;
    auto pass = [](DesignState& s, int) {
        // Advance geometry so memory/no_geometry guard does not fail the run.
        s.revision += 1;
        s.tree_digest = "f1:extrude";
        StageOutcome o;
        o.verdict = "pass";
        return o;
    };
    actions.intake = pass;
    actions.plan = pass;  // unused when plan budget is 0
    actions.model = pass;
    actions.review = pass;
    actions.fix = pass;
    actions.memory = pass;
    actions.plan_steps = [](DesignState&) { return std::vector<std::string>{""}; };

    std::vector<json> seen;
    RunEvents events("plan-steps-solo-blank", [&](const json& ev) { seen.push_back(ev); });
    json budgets = {{"plan", 0}, {"model", 1}, {"review", 1}, {"fix", 1}, {"memory", 1},
                    {"intake", 1}};
    json result = run_machine(st, actions, events, budgets);
    CHECK(result["status"] == "done");

    int n_plan_steps = 0, n_model_checklist = 0, n_stage_checklist = 0;
    for (const auto& ev : seen) {
        const std::string kind = ev.value("kind", std::string());
        if (kind == "plan_steps") ++n_plan_steps;
        if (kind != "plan_state") continue;
        if (ev.value("shape", "") != "checklist") continue;
        // model:N checklist (ids model:0, model:1, …) vs solo stage checklist (intake:/model:/…).
        const std::string title = ev.value("title", std::string());
        bool has_model_step_id = false;
        if (ev.contains("items") && ev["items"].is_array()) {
            for (const auto& it : ev["items"]) {
                if (!it.is_object()) continue;
                const std::string id = it.value("id", std::string());
                // model:N only — bare "model:" is the solo stage checklist id.
                if (id.rfind("model:", 0) == 0 && id.size() > 6) {
                    bool all_digit = true;
                    for (size_t k = 6; k < id.size(); ++k)
                        if (id[k] < '0' || id[k] > '9') { all_digit = false; break; }
                    if (all_digit) { has_model_step_id = true; break; }
                }
            }
        }
        if (has_model_step_id || title == "Model steps")
            ++n_model_checklist;
        else
            ++n_stage_checklist;
    }
    CHECK(n_plan_steps == 1);       // legacy raw list kept when no model:N plan_state
    CHECK(n_model_checklist == 0);  // blank step → no model-step checklist
    CHECK(n_stage_checklist >= 1);  // solo stage checklist still surfaces
}

TEST_CASE("extract_ops_last_nonempty: reasoning-channel ops fallback") {
    // grok-4.5 leaves the content channel empty/`[]` and writes the real ops in reasoning.
    // The fallback must skip DRAFT arrays + coordinate fragments and return the FINAL block.
    SUBCASE("content bare [] yields nothing; reasoning last fenced block wins") {
        // extract_ops on a bare empty array returns an (empty) ops array -> caller checks empty.
        OpsResult content = extract_ops("```json\n[]\n```");
        CHECK((!content.ops || content.ops->empty()));
        std::string reasoning =
            "Let me sketch it. First a draft:\n"
            "```json\n[{\"op\":\"add_feature\",\"type\":\"sketch\"}]\n```\n"
            "Actually the points are [-8, 0], [-4, 6.9] ... let me redo.\n"
            "Final answer:\n"
            "```json\n[{\"op\":\"add_feature\",\"type\":\"sketch\"},"
            "{\"op\":\"sketch_add_entity\",\"type\":\"circle\",\"radius\":10},"
            "{\"op\":\"add_feature\",\"type\":\"extrude\"}]\n```\n";
        OpsResult r = extract_ops_last_nonempty(reasoning);
        REQUIRE(r.ops.has_value());
        CHECK(r.ops->size() == 3);  // the LAST fenced block, not the 1-op draft
        CHECK((*r.ops)[1]["type"] == "circle");
    }
    SUBCASE("coordinate fragments never match as ops") {
        // No fenced block; only a bare coordinate array present -> must NOT be taken as ops.
        OpsResult r = extract_ops_last_nonempty("the hexagon corner is at [-8, 0] across flats");
        CHECK_FALSE(r.ops.has_value());
    }
    SUBCASE("empty reasoning -> no ops") {
        OpsResult r = extract_ops_last_nonempty("just prose, no json at all");
        CHECK_FALSE(r.ops.has_value());
    }
    SUBCASE("bare fenced ops with no draft still resolves") {
        OpsResult r = extract_ops_last_nonempty(
            "```json\n[{\"op\":\"add_feature\",\"type\":\"box\"}]\n```");
        REQUIRE(r.ops.has_value());
        CHECK(r.ops->size() == 1);
    }
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

// --------------------------------------------------------------------------- harness fixes (battery r1)
namespace {

StageOutcome pass_outcome(const char* reason = "ok") {
    StageOutcome o;
    o.verdict = "pass";
    o.reason = reason;
    return o;
}

StageActions bare_pass_actions() {
    StageActions a;
    auto pass = [](DesignState&, int) { return pass_outcome(); };
    a.intake = pass;
    a.plan = pass;
    a.model = [](DesignState& s, int) {
        s.revision += 1;
        s.tree_digest = "f1:box";
        return pass_outcome("modeled");
    };
    a.review = pass;
    a.fix = pass;
    a.memory = pass;
    a.plan_steps = [](DesignState&) { return std::vector<std::string>{""}; };
    return a;
}

}  // namespace

TEST_CASE("run_machine intake reject stops with unintelligible_request") {
    DesignState st;
    st.request = "asdf qwerty blah blah";
    int plan_n = 0, model_n = 0;
    StageActions actions = bare_pass_actions();
    actions.intake = [](DesignState&, int) {
        StageOutcome o;
        o.verdict = "fail";
        o.reason = "not a CAD design request";
        o.has_error = true;
        o.error = "unintelligible_request";
        o.parsed_ok = true;
        return o;
    };
    actions.plan = [&](DesignState&, int) {
        ++plan_n;
        return pass_outcome();
    };
    actions.model = [&](DesignState&, int) {
        ++model_n;
        return pass_outcome();
    };

    std::vector<json> seen;
    RunEvents events("intake-reject-ut", [&](const json& ev) { seen.push_back(ev); });
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 1}, {"model", 1},
                                   {"review", 1}, {"fix", 1}, {"memory", 1}});
    CHECK(result["status"] == "failed");
    CHECK(result["stop_reason"] == "unintelligible_request");
    CHECK(result.value("error_detail", std::string()) == "not a CAD design request");
    CHECK(plan_n == 0);
    CHECK(model_n == 0);
    // stage_result for intake fail was emitted by the machine.
    bool saw_intake_fail = false;
    for (const auto& ev : seen) {
        if (ev.value("kind", "") == "stage_result" && ev.value("stage", "") == "intake" &&
            ev.value("verdict", "") == "fail")
            saw_intake_fail = true;
    }
    CHECK(saw_intake_fail);
}

TEST_CASE("run_machine empty normalized request rejects same path") {
    DesignState st;
    st.request = "   ";
    int plan_n = 0, model_n = 0;
    StageActions actions = bare_pass_actions();
    actions.intake = [](DesignState&, int) {
        // Mirrors run.cpp empty-spec belt: fail + unintelligible_request.
        StageOutcome o;
        o.verdict = "fail";
        o.reason = "normalized request is empty";
        o.has_error = true;
        o.error = "unintelligible_request";
        o.parsed_ok = true;
        return o;
    };
    actions.plan = [&](DesignState&, int) {
        ++plan_n;
        return pass_outcome();
    };
    actions.model = [&](DesignState&, int) {
        ++model_n;
        return pass_outcome();
    };

    RunEvents events("intake-empty-ut", nullptr);
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 1}, {"model", 1},
                                   {"review", 1}, {"fix", 1}, {"memory", 1}});
    CHECK(result["status"] == "failed");
    CHECK(result["stop_reason"] == "unintelligible_request");
    CHECK(result.value("error_detail", std::string()) == "normalized request is empty");
    CHECK(plan_n == 0);
    CHECK(model_n == 0);
}

TEST_CASE("run_machine mod request on doc with features reaches model") {
    // Edit follow-up on an open part: intake returns a normalized edit (not reject).
    // Model stage must run — the object is the open doc.
    DesignState st;
    st.request = "chamfer the top edges 0.5mm";
    st.revision = 4;
    st.tree_digest = "f1 extrude Washer\nf2 cut Bore";
    int model_n = 0;
    StageActions actions = bare_pass_actions();
    actions.intake = [](DesignState& s, int) {
        StageOutcome o;
        o.verdict = "pass";
        o.reason = "spec parsed";
        o.parsed_ok = true;
        s.spec = ojson::parse(R"({
            "object": "existing washer",
            "key_dims": {"chamfer": 0.5},
            "features": ["chamfer top edges"],
            "constraints": []
        })");
        return o;
    };
    actions.model = [&](DesignState& s, int) {
        ++model_n;
        s.revision += 1;
        s.tree_digest = "f1 extrude Washer\nf2 cut Bore\nf3 chamfer";
        return pass_outcome("modeled");
    };

    RunEvents events("intake-mod-with-geom-ut", nullptr);
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 1}, {"model", 1},
                                   {"review", 1}, {"fix", 1}, {"memory", 1}});
    CHECK(result["status"] == "done");
    CHECK(model_n == 1);
}

TEST_CASE("run_machine garbage on empty doc terminates unintelligible") {
    DesignState st;
    st.request = "asdf qwerty";
    st.revision = 0;
    st.tree_digest = "(empty — no features yet)";
    int plan_n = 0, model_n = 0;
    StageActions actions = bare_pass_actions();
    actions.intake = [](DesignState&, int) {
        StageOutcome o;
        o.verdict = "fail";
        o.reason = "not a CAD design request";
        o.has_error = true;
        o.error = "unintelligible_request";
        o.parsed_ok = true;
        return o;
    };
    actions.plan = [&](DesignState&, int) {
        ++plan_n;
        return pass_outcome();
    };
    actions.model = [&](DesignState&, int) {
        ++model_n;
        return pass_outcome();
    };

    RunEvents events("intake-garbage-empty-ut", nullptr);
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 1}, {"model", 1},
                                   {"review", 1}, {"fix", 1}, {"memory", 1}});
    CHECK(result["status"] == "failed");
    CHECK(result["stop_reason"] == "unintelligible_request");
    CHECK(result.value("error_detail", std::string()) == "not a CAD design request");
    CHECK(plan_n == 0);
    CHECK(model_n == 0);
}

TEST_CASE("run_machine chamfer on empty doc rejects nothing to modify") {
    // Modification request with no existing geometry: intake rejects with an explicit reason.
    DesignState st;
    st.request = "chamfer the edges";
    st.revision = 0;
    st.tree_digest = "(empty — no features yet)";
    int plan_n = 0, model_n = 0;
    StageActions actions = bare_pass_actions();
    actions.intake = [](DesignState&, int) {
        StageOutcome o;
        o.verdict = "fail";
        o.reason = "no existing geometry to modify";
        o.has_error = true;
        o.error = "unintelligible_request";
        o.parsed_ok = true;
        return o;
    };
    actions.plan = [&](DesignState&, int) {
        ++plan_n;
        return pass_outcome();
    };
    actions.model = [&](DesignState&, int) {
        ++model_n;
        return pass_outcome();
    };

    RunEvents events("intake-chamfer-empty-ut", nullptr);
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 1}, {"model", 1},
                                   {"review", 1}, {"fix", 1}, {"memory", 1}});
    CHECK(result["status"] == "failed");
    CHECK(result["stop_reason"] == "unintelligible_request");
    CHECK(result.value("error_detail", std::string()) == "no existing geometry to modify");
    CHECK(plan_n == 0);
    CHECK(model_n == 0);
}

TEST_CASE("run_machine capability McMaster request is not unintelligible") {
    // Mirrors the production override: when the LLM wrongly rejects a parts-library /
    // McMaster capability request, the host accepts it (is_capability_request) and the
    // machine continues past intake. Here we assert the ACCEPT path: intake pass with
    // a capability-shaped spec reaches plan/model (not unintelligible_request).
    DesignState st;
    st.request = "download a model from McMaster to test tooling for the parts library";
    int plan_n = 0, model_n = 0;
    StageActions actions = bare_pass_actions();
    actions.intake = [](DesignState& s, int) {
        // Simulate the capability override path in run.cpp intake.
        StageOutcome o;
        o.verdict = "pass";
        o.reason = "capability request accepted: capability: parts-library/McMaster fetch";
        o.parsed_ok = true;
        s.spec = ojson::parse(R"({
            "object": "capability request",
            "key_dims": {},
            "features": ["capability: parts-library/McMaster fetch"],
            "constraints": [],
            "capability": true
        })");
        return o;
    };
    actions.plan = [&](DesignState&, int) {
        ++plan_n;
        return pass_outcome();
    };
    actions.model = [&](DesignState& s, int) {
        ++model_n;
        s.revision += 1;
        return pass_outcome("modeled");
    };

    RunEvents events("intake-mcmaster-cap-ut", nullptr);
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 1}, {"model", 1},
                                   {"review", 1}, {"fix", 1}, {"memory", 1}});
    CHECK(result["status"] == "done");
    CHECK(result.value("stop_reason", std::string()) != "unintelligible_request");
    CHECK(plan_n == 1);
    CHECK(model_n == 1);
}

TEST_CASE("run_machine reject still surfaces classifier reason") {
    // Transparency: error_detail must carry the intake reason, not a bare code.
    DesignState st;
    st.request = "zzzz not real";
    StageActions actions = bare_pass_actions();
    actions.intake = [](DesignState&, int) {
        StageOutcome o;
        o.verdict = "fail";
        o.reason = "gibberish / non-CAD content";
        o.has_error = true;
        o.error = "unintelligible_request";
        o.parsed_ok = true;
        return o;
    };
    RunEvents events("intake-reason-surface-ut", nullptr);
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 1}, {"model", 1},
                                   {"review", 1}, {"fix", 1}, {"memory", 1}});
    CHECK(result["status"] == "failed");
    CHECK(result["stop_reason"] == "unintelligible_request");
    CHECK(result.value("error_detail", std::string()) == "gibberish / non-CAD content");
    CHECK(result.value("error_human", std::string()) == "gibberish / non-CAD content");
}


TEST_CASE("run_machine solo_skip_plan omits plan action entirely") {
    DesignState st;
    st.request = "50mm cube";
    int plan_n = 0;
    StageActions actions = bare_pass_actions();
    actions.plan = [&](DesignState&, int) {
        ++plan_n;
        return pass_outcome("should not run");
    };

    std::vector<json> seen;
    RunEvents events("solo-skip-plan-ut", [&](const json& ev) { seen.push_back(ev); });
    // budgets.plan=0 is the solo_skip_plan contract from run.cpp.
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 0}, {"model", 1},
                                   {"review", 1}, {"fix", 2}, {"memory", 1}});
    CHECK(result["status"] == "done");
    CHECK(plan_n == 0);
    // No plan stage_started / stage_result events.
    for (const auto& ev : seen) {
        const std::string kind = ev.value("kind", std::string());
        if (kind == "stage_started" || kind == "stage_result")
            CHECK(ev.value("stage", std::string()) != "plan");
    }
    // Deterministic stage checklist (no LLM) with namespaced ids.
    bool saw_stage_checklist = false;
    json last_check;
    for (const auto& ev : seen) {
        if (ev.value("kind", "") != "plan_state") continue;
        if (ev.value("shape", "") != "checklist") continue;
        saw_stage_checklist = true;
        last_check = ev;
    }
    CHECK(saw_stage_checklist);
    REQUIRE(last_check.is_object());
    REQUIRE(last_check["items"].is_array());
    bool has_model = false, has_review = false, has_memory = false;
    for (const auto& it : last_check["items"]) {
        const std::string id = it.value("id", std::string());
        if (id == "model:") {
            has_model = true;
            CHECK(it["status"] == "done");
        } else if (id == "review:") {
            has_review = true;
            CHECK(it["status"] == "done");
        } else if (id == "memory:") {
            has_memory = true;
            CHECK(it["status"] == "done");
        }
        // Must not collide with mission:/firm: namespaces.
        CHECK(id.rfind("mission:", 0) != 0);
        CHECK(id.rfind("firm:", 0) != 0);
    }
    CHECK(has_model);
    CHECK(has_review);
    CHECK(has_memory);
}

TEST_CASE("run_machine fix budget is a run-level cap under always-fail review") {
    DesignState st;
    st.request = "bracket";
    int fix_n = 0;
    const int kFixBudget = 2;  // Solo default
    StageActions actions = bare_pass_actions();
    actions.review = [](DesignState&, int) {
        StageOutcome o;
        o.verdict = "retry";
        o.reason = "review: 1 issue(s)";
        o.parsed_ok = true;
        return o;
    };
    actions.fix = [&](DesignState&, int) {
        ++fix_n;
        // Pass so the loop would re-review; the run-level cap must still hold.
        return pass_outcome("fixed");
    };

    RunEvents events("fix-budget-ut", nullptr);
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 0}, {"model", 1},
                                   {"review", 8}, {"fix", kFixBudget}, {"memory", 1}});
    // Run completes (memory names the part) even if review never passed.
    CHECK(result["status"] == "done");
    CHECK(fix_n == kFixBudget);
}

TEST_CASE("run_machine first review pass runs zero fix iterations") {
    // Documents that review early-exit already worked (and still does).
    DesignState st;
    st.request = "washer";
    int fix_n = 0;
    StageActions actions = bare_pass_actions();
    actions.review = [](DesignState&, int) { return pass_outcome("review: done"); };
    actions.fix = [&](DesignState&, int) {
        ++fix_n;
        return pass_outcome();
    };
    RunEvents events("review-early-exit-ut", nullptr);
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 0}, {"model", 1},
                                   {"review", 4}, {"fix", 6}, {"memory", 1}});
    CHECK(result["status"] == "done");
    CHECK(fix_n == 0);
}

TEST_CASE("DesignState domain seed grants sheetmetal tool_scope_classes") {
    // Mirrors run_design's post-classify seed (Domain::Sheetmetal → "sheetmetal").
    DesignState st;
    st.request = "sheetmetal enclosure 200x120x60mm";
    DomainVerdict dom = classify_domain(st.request);
    CHECK(dom.domain == Domain::Sheetmetal);
    st.domain = dom.domain;
    st.domain_set = true;
    if (dom.domain == Domain::Sheetmetal) st.tool_scope_classes.insert("sheetmetal");
    CHECK(st.tool_scope_classes.count("sheetmetal") == 1);
    // Modeling does not seed a domain class.
    DesignState m;
    m.request = "50mm cube";
    DomainVerdict md = classify_domain(m.request);
    CHECK(md.domain == Domain::Modeling);
    m.domain = md.domain;
    m.domain_set = true;
    // no seed for Modeling
    CHECK(m.tool_scope_classes.empty());
}

TEST_CASE("assembly_plausible gate: single-part skips probe, multi-part probes") {
    // Pure classifier contract used by run_assembly_design's skip-the-probe path.
    auto plausible = [](const std::string& req) {
        auto cx = classify_complexity(req);
        auto dm = classify_domain(req);
        return cx.level == Complexity::Complex || dm.domain == Domain::Assembly;
    };
    CHECK_FALSE(plausible("50mm cube"));
    CHECK_FALSE(plausible("sheetmetal enclosure 200x120x60mm, 1.5mm steel"));
    CHECK_FALSE(plausible("M6 washer 12mm OD 1mm thick"));
    CHECK(plausible("two brackets that bolt together with M5"));
    CHECK(plausible("assemble the gearbox and add mates"));
}

// ---------------------------------------------------------------------------
// Exec-mode semantics (advisor / delegate / peels / structure / budget)
// ---------------------------------------------------------------------------

TEST_CASE("peel_request_advice extracts question and strips meta-ops") {
    json ops = json::array({
        json{{"op", "request_advice"}, {"question", "wall thickness?"}, {"context", "FDM"}},
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}}}},
    });
    AdviceRequest ar = peel_request_advice(ops);
    CHECK(ar.had_request);
    CHECK(ar.question == "wall thickness?");
    CHECK(ar.context == "FDM");
    REQUIRE(ar.remaining.is_array());
    CHECK(ar.remaining.size() == 1);
    CHECK(ar.remaining[0]["op"] == "add_feature");
}

TEST_CASE("peel_delegate extracts task and why") {
    json ops = json::array({
        json{{"op", "delegate"}, {"task", "add mounting holes"}, {"why", "sub-scope"}},
    });
    DelegateRequest dr = peel_delegate(ops);
    CHECK(dr.had_request);
    CHECK(dr.task == "add mounting holes");
    CHECK(dr.why == "sub-scope");
    CHECK(dr.remaining.empty());
}

TEST_CASE("parse_advisor_plan_verdict approve revise escalate") {
    {
        auto v = parse_advisor_plan_verdict("```json\n{\"verdict\":\"approve\",\"note\":\"ok\"}\n```");
        CHECK(v.ok);
        CHECK(v.verdict == "approve");
        CHECK(v.note == "ok");
    }
    {
        auto v = parse_advisor_plan_verdict(
            "{\"verdict\":\"revise\",\"note\":\"split the gearbox housing\"}");
        CHECK(v.ok);
        CHECK(v.verdict == "revise");
        CHECK(v.note.find("gearbox") != std::string::npos);
    }
    {
        auto v = parse_advisor_plan_verdict(
            "{\"verdict\":\"escalate\",\"note\":\"pick aluminum or steel\"}");
        CHECK(v.ok);
        CHECK(v.verdict == "escalate");
    }
    {
        // Unparseable → approve (do not block the run) + honest fallback stamp.
        auto v = parse_advisor_plan_verdict("not json at all");
        CHECK_FALSE(v.ok);
        CHECK(v.verdict == "approve");
        CHECK(v.fallback == "unparseable");
        CHECK(v.note.find("unparseable") != std::string::npos);
    }
    {
        // Unrecognized verdict → soft-approve with fallback.
        auto v = parse_advisor_plan_verdict("{\"verdict\":\"maybe\",\"note\":\"hmm\"}");
        CHECK_FALSE(v.ok);
        CHECK(v.verdict == "approve");
        CHECK(v.fallback == "unparseable");
    }
}

TEST_CASE("parse_advisor_advice confidence") {
    auto a = parse_advisor_advice(
        "```json\n{\"advice\":\"use 2mm walls\",\"confidence\":\"high\"}\n```");
    CHECK(a.ok);
    CHECK(a.advice == "use 2mm walls");
    CHECK(a.confidence == "high");
}

TEST_CASE("execution_style_from_structure maps structure labels") {
    CHECK(std::string(execution_style_from_structure("solo")) == "edit");
    CHECK(std::string(execution_style_from_structure("subagent")) == "edit");
    CHECK(std::string(execution_style_from_structure("dag")) == "swarm");
    CHECK(std::string(execution_style_from_structure("company")) == "swarm");
    // recommend_structure → style chain
    auto solo = recommend_structure(Complexity::Solo);
    CHECK(std::string(execution_style_from_structure(structure_label(solo))) == "edit");
    auto complex = recommend_structure(Complexity::Complex, /*cross_discipline=*/false);
    CHECK(std::string(execution_style_from_structure(structure_label(complex))) == "swarm");
    // company requires allow_company=true; still maps to swarm execution_style
    auto company = recommend_structure(Complexity::Complex, /*cross_discipline=*/true,
                                      /*allow_company=*/true);
    CHECK(std::string(structure_label(company)) == "company");
    CHECK(std::string(execution_style_from_structure(structure_label(company))) == "swarm");
    // cross_discipline alone stays dag (company opt-in)
    auto dag_xd = recommend_structure(Complexity::Complex, /*cross_discipline=*/true);
    CHECK(std::string(structure_label(dag_xd)) == "dag");
}

TEST_CASE("run_machine advisor escalate returns plan_ready needs_user_decision") {
    DesignState st;
    st.request = "ambiguous mechanism";
    st.execution_style = "edit";
    st.plan = ojson{{"name", "Mech"},
                    {"summary", "unclear"},
                    {"parts", ojson::array({ojson{{"name", "body"},
                                                  {"purpose", "main"},
                                                  {"dimensions", ojson::object()},
                                                  {"features", ojson::array()}}})}};

    int model_n = 0;
    StageActions actions = bare_pass_actions();
    actions.plan = [&](DesignState& s, int) {
        // Simulate advisor escalate after drafting (plan already on state).
        (void)s;
        StageOutcome o;
        o.verdict = "fail";
        o.reason = "pick aluminum or steel";
        o.has_error = true;
        o.error = "needs_user_decision";
        o.parsed_ok = true;
        return o;
    };
    actions.model = [&](DesignState&, int) {
        ++model_n;
        return pass_outcome();
    };

    std::vector<json> seen;
    RunEvents events("adv-escalate-ut", [&](const json& ev) { seen.push_back(ev); });
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 1}, {"model", 1},
                                   {"review", 1}, {"fix", 1}, {"memory", 1}});
    CHECK(result["status"] == "plan_ready");
    CHECK(result["stop_reason"] == "needs_user_decision");
    CHECK(result.value("reason", std::string()) == "pick aluminum or steel");
    CHECK(result.contains("plan"));
    CHECK(result.value("execution_style", std::string()) == "edit");
    CHECK(model_n == 0);  // must not proceed to model
}

TEST_CASE("run_machine advisor approve continues into model") {
    DesignState st;
    st.request = "bracket";
    st.execution_style = "edit";
    int model_n = 0;
    StageActions actions = bare_pass_actions();
    actions.plan = [](DesignState& s, int) {
        s.plan = ojson{{"name", "Bracket"},
                       {"summary", "ok"},
                       {"parts", ojson::array({ojson{{"name", "body"},
                                                     {"purpose", "main"},
                                                     {"dimensions", ojson{{"l", 60}}},
                                                     {"features", ojson::array()}}})}};
        StageOutcome o;
        o.verdict = "pass";
        o.reason = "plan parsed (advisor approved)";
        o.parsed_ok = true;
        return o;
    };
    actions.model = [&](DesignState& s, int) {
        ++model_n;
        s.revision += 1;
        s.tree_digest = "f1:box";
        return pass_outcome("modeled");
    };
    RunEvents events("adv-approve-ut", nullptr);
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 1}, {"model", 1},
                                   {"review", 1}, {"fix", 1}, {"memory", 1}});
    CHECK(result["status"] == "done");
    CHECK(model_n == 1);
}

TEST_CASE("run_machine advisor revise one pass then continue") {
    // Pure seam: plan action does one internal revision then passes (mirrors run.cpp).
    DesignState st;
    st.request = "gearbox";
    int plan_calls = 0;
    int model_n = 0;
    StageActions actions = bare_pass_actions();
    actions.plan = [&](DesignState& s, int) {
        ++plan_calls;
        // First "draft"
        s.plan = ojson{{"name", "Gearbox"},
                       {"summary", "v1"},
                       {"parts", ojson::array({ojson{{"name", "housing"},
                                                     {"purpose", "case"},
                                                     {"dimensions", ojson::object()},
                                                     {"features", ojson::array()}}})}};
        // Simulate revise → one rewrite → continue (no second review).
        s.plan["summary"] = "v2 revised";
        StageOutcome o;
        o.verdict = "pass";
        o.reason = "plan revised per advisor";
        o.parsed_ok = true;
        return o;
    };
    actions.model = [&](DesignState& s, int) {
        ++model_n;
        s.revision += 1;
        s.tree_digest = "f1:box";
        return pass_outcome();
    };
    RunEvents events("adv-revise-ut", nullptr);
    json result = run_machine(st, actions, events,
                              json{{"intake", 1}, {"plan", 1}, {"model", 1},
                                   {"review", 1}, {"fix", 1}, {"memory", 1}});
    CHECK(result["status"] == "done");
    CHECK(plan_calls == 1);  // one plan stage invocation (revision is inside the Action)
    CHECK(model_n == 1);
    CHECK(st.plan["summary"] == "v2 revised");
}

TEST_CASE("request_advice peel cap is 3") {
    // Pure counter seam mirroring run.cpp handle_request_advice.
    DesignState st;
    st.advisor_calls = 0;
    constexpr int kCap = 3;
    auto honor = [&](bool had) -> bool {
        if (!had) return false;
        if (st.advisor_calls >= kCap) return false;  // ignored
        ++st.advisor_calls;
        return true;
    };
    CHECK(honor(true));
    CHECK(honor(true));
    CHECK(honor(true));
    CHECK_FALSE(honor(true));  // 4th ignored
    CHECK(st.advisor_calls == 3);
}

TEST_CASE("delegate depth cap and Solo refusal") {
    auto refuse = [](const DesignState& st, const std::string& mode) -> std::string {
        const bool mode_ok = mode.empty() || mode == "edit" || mode == "plan" || mode == "agent";
        if (!mode_ok) return "mode";
        if (st.complexity_set && st.complexity == Complexity::Solo) return "solo";
        if (st.is_sub_run || st.run_depth >= 2) return "depth";
        return "";
    };
    DesignState solo;
    solo.complexity = Complexity::Solo;
    solo.complexity_set = true;
    CHECK(refuse(solo, "edit") == "solo");

    DesignState deep;
    deep.complexity = Complexity::Standard;
    deep.complexity_set = true;
    deep.run_depth = 2;
    CHECK(refuse(deep, "agent") == "depth");

    DesignState sub;
    sub.complexity = Complexity::Standard;
    sub.complexity_set = true;
    sub.is_sub_run = true;
    CHECK(refuse(sub, "agent") == "depth");

    DesignState ok;
    ok.complexity = Complexity::Standard;
    ok.complexity_set = true;
    ok.run_depth = 0;
    CHECK(refuse(ok, "agent").empty());
    CHECK(refuse(ok, "swarm") == "mode");
}

TEST_CASE("swarm plan gate requires approved_plan or advisor path") {
    // Pure contract: mode swarm/eng_firm without approved_plan must NOT jump to execution.
    auto needs_gate = [](const std::string& mode, bool has_ap) {
        const bool style_gate = (mode == "swarm" || mode == "eng_firm");
        return style_gate && !has_ap;
    };
    CHECK(needs_gate("swarm", false));
    CHECK(needs_gate("eng_firm", false));
    CHECK_FALSE(needs_gate("swarm", true));
    CHECK_FALSE(needs_gate("agent", false));
    CHECK_FALSE(needs_gate("edit", false));
}

TEST_CASE("eng_firm selects cheapest in-scope token bid") {
    auto bids =
        collect_bids("two brackets that bolt together", Complexity::Complex, Domain::Assembly);
    REQUIRE_FALSE(bids.empty());
    int in_scope = 0;
    for (const auto& b : bids) {
        if (b.in_scope) ++in_scope;
        // Every bid must carry a non-empty firm name (firm_award.firm source).
        CHECK_FALSE(b.firm_name.empty());
        CHECK_FALSE(b.firm_id.empty());
        CHECK(b.tokens > 0);
    }
    CHECK(in_scope > 0);

    auto pick_award = [](const std::vector<Bid>& bs) -> const Bid* {
        for (const auto& b : bs)
            if (b.in_scope) return &b;
        return bs.empty() ? nullptr : &bs.front();
    };
    const Bid* win = pick_award(bids);
    REQUIRE(win != nullptr);
    CHECK(win->in_scope);
    CHECK_FALSE(win->firm_name.empty());
    // Sorted cheapest-first among in-scope firms.
    for (const auto& b : bids)
        if (b.in_scope) CHECK(win->tokens <= b.tokens);
}

TEST_CASE("a silent worker is not a non-modeling request") {
    // run-42508173312214 (2026-07-29): a keychain modelling turn built five real,
    // auto-persisted features, then stopped responding. The host filed it as
    // stop_reason "non_modeling_request" / status "blocked" and told the user
    // "nothing was changed" — while the geometry sat in the feature tree, which is
    // what made the user retry and end up with duplicate parts.
    // Unlike the mirrored lambdas above, this LINKS the shipped rule.
    using chudware::agent::classify_worker_stop;

    const auto silent = classify_worker_stop(/*crashed=*/false, /*timed_out=*/true,
                                             /*reached_worker=*/true);
    CHECK(silent.stop_reason == "worker_timeout");
    CHECK(silent.stop_reason != "non_modeling_request");
    CHECK(silent.status == "failed");
    // The honesty half: the run committed ops through the engine as it went.
    CHECK(silent.may_have_changed_the_model);

    const auto died = classify_worker_stop(true, false, true);
    CHECK(died.stop_reason == "worker_crash");
    CHECK(died.status == "failed");
    CHECK(died.may_have_changed_the_model);

    // A worker killed on the timeout path still reports its signal; the crash is the
    // more specific fact, so it must win rather than being masked by the timeout.
    CHECK(classify_worker_stop(true, true, true).stop_reason == "worker_crash");

    // The offline pre-flight never reaches the worker — the ONE case that may honestly
    // promise the model is untouched, and the only one that stays "blocked".
    const auto offline = classify_worker_stop(false, false, /*reached_worker=*/false);
    CHECK(offline.stop_reason == "non_modeling_request");
    CHECK(offline.status == "blocked");
    CHECK_FALSE(offline.may_have_changed_the_model);

    // A reached worker that rejects its request is a harness failure, not evidence
    // that the user's request was non-modeling.
    const auto declined = classify_worker_stop(false, false, true);
    CHECK(declined.stop_reason == "worker_error");
    CHECK(declined.status == "failed");
    CHECK(declined.may_have_changed_the_model);
}

TEST_CASE("a considered decline is not a crash, and keeps its reason") {
    // Live 2026-08-27 (pharm-press-2): a machine-shop tooling request ran research,
    // then the model declined through the harness's own handoff — status "blocked",
    // a paragraph of reasoning, three offered alternatives. It reached the GUI as
    // status=failed with error_detail=null, so the chat card printed
    // "I stopped before fully finishing (blocked). Whatever was built so far is in
    // the FeatureManager tree" — a wrong label, a false claim (nothing was built),
    // and not one word of the actual reason.
    using chudware::agent::general_lane_error_detail;
    using chudware::agent::general_lane_status;

    CHECK(general_lane_status(false, "blocked", "blocked") == "blocked");
    CHECK(general_lane_status(false, "escalated", "blocked") == "blocked");
    // Unchanged mappings.
    CHECK(general_lane_status(true, "done", "confirmed") == "done");
    CHECK(general_lane_status(false, "partial", "max_iters") == "partial");
    CHECK(general_lane_status(false, "", "guardrail_blocked") == "blocked");
    // A genuine fault must still read as one — this is not "everything is blocked".
    CHECK(general_lane_status(false, "", "provider_error") == "failed");
    CHECK(general_lane_status(false, "", "worker_crash") == "failed");
    // `partial` outranks a blocked handoff: geometry exists and the host renders it.
    CHECK(general_lane_status(false, "partial", "guardrail_blocked") == "partial");

    // The reason survives into error_detail, which events.jsonl records.
    const std::string why = "Declined to produce the tooling package; offered three "
                            "alternatives.";
    CHECK(general_lane_error_detail("", "blocked", why) == why);
    // A detail the turn built itself always wins over the summary.
    CHECK(general_lane_error_detail("HTTP 401", "failed", why) == "HTTP 401");
    // A successful run has nothing to explain.
    CHECK(general_lane_error_detail("", "done", why).empty());
    CHECK(general_lane_error_detail("", "blocked", "").empty());

    // Long summaries are clipped to the contract's ~300-char sentence, on a UTF-8
    // boundary — a mid-sequence cut would put an invalid byte in the event stream.
    const std::string wide(150, 'x');
    std::string mb = wide;
    for (int i = 0; i < 80; ++i) mb += "\xC3\xA9";  // é, 2 bytes each
    const std::string clipped = general_lane_error_detail("", "blocked", mb);
    CHECK(clipped.size() <= 302);          // <=299 payload + 3-byte ellipsis
    CHECK(clipped.size() > 290);
    for (size_t i = 0; i < clipped.size();) {
        const unsigned char c = static_cast<unsigned char>(clipped[i]);
        const size_t len = c < 0x80 ? 1 : (c >> 5) == 0x6 ? 2 : (c >> 4) == 0xE ? 3 : 4;
        REQUIRE(i + len <= clipped.size());  // no truncated code point
        for (size_t k = 1; k < len; ++k)
            CHECK((static_cast<unsigned char>(clipped[i + k]) & 0xC0) == 0x80);
        i += len;
    }
}

TEST_CASE("mission stop_reason propagates real unit failure") {
    // Mirrors run_mission finish: never emit opaque "planned" on failure;
    // mixed success+failure → partial (not done/built).
    struct Outcome {
        std::string status;
        std::string stop_reason;
    };
    auto resolve = [](bool any_built, bool any_ok, bool created_doc, bool any_failed,
                      const std::string& first_fail,
                      const std::vector<std::string>& fail_summaries) -> Outcome {
        auto fail_r = [&]() -> std::string {
            if (!first_fail.empty()) return first_fail;
            for (auto it = fail_summaries.rbegin(); it != fail_summaries.rend(); ++it)
                if (!it->empty()) return *it;
            return "mission_failed";
        };
        if (any_failed && (any_built || any_ok))
            return {"partial", fail_r()};
        if (any_built && !any_failed)
            return {"done", created_doc ? "assembled" : "built"};
        if (any_ok && !any_failed) return {"done", "done"};
        return {"failed", fail_r()};
    };
    // All-success build / assemble.
    {
        auto o = resolve(true, true, false, false, "", {});
        CHECK(o.status == "done");
        CHECK(o.stop_reason == "built");
    }
    {
        auto o = resolve(true, true, true, false, "", {});
        CHECK(o.status == "done");
        CHECK(o.stop_reason == "assembled");
    }
    // Total failure surfaces real cause (never "planned").
    {
        auto o = resolve(false, false, false, true, "provider_error", {"provider_error"});
        CHECK(o.status == "failed");
        CHECK(o.stop_reason == "provider_error");
        CHECK(o.stop_reason != "planned");
    }
    {
        auto o = resolve(false, false, false, true, "no_geometry", {});
        CHECK(o.status == "failed");
        CHECK(o.stop_reason == "no_geometry");
    }
    {
        auto o = resolve(false, false, false, true, "", {"", "parse_error"});
        CHECK(o.status == "failed");
        CHECK(o.stop_reason == "parse_error");
    }
}

TEST_CASE("mission partial when some tasks fail after success") {
    // Live bug: u1 design done + u2 assembly model_failed must NOT be done/built.
    // status=partial preserves u1 geometry; stop_reason carries u2's failure.
    struct Outcome {
        std::string status;
        std::string stop_reason;
        int completed = 0;
        int failed = 0;
    };
    auto finish = [](const std::vector<std::pair<std::string, std::string>>& tasks) -> Outcome {
        // tasks: {status, summary}
        Outcome o;
        bool any_built = false;
        bool any_ok = false;
        std::string first_fail;
        for (const auto& t : tasks) {
            if (t.first == "done") {
                ++o.completed;
                any_ok = true;
                any_built = true;  // design/assembly done counts as built work
            } else if (t.first != "abandoned" && t.first != "pending" && t.first != "skipped") {
                ++o.failed;
                if (first_fail.empty())
                    first_fail = !t.second.empty() ? t.second : t.first;
            }
        }
        const bool any_failed = o.failed > 0;
        if (any_failed && (any_built || any_ok || o.completed > 0)) {
            o.status = "partial";
            o.stop_reason = first_fail.empty() ? "mission_failed" : first_fail;
        } else if (any_built && !any_failed) {
            o.status = "done";
            o.stop_reason = "built";
        } else if (any_ok && !any_failed) {
            o.status = "done";
            o.stop_reason = "done";
        } else {
            o.status = "failed";
            o.stop_reason = first_fail.empty() ? "mission_failed" : first_fail;
        }
        return o;
    };

    // The spot failure: u1 done, u2 model_failed.
    {
        auto o = finish({{"done", "built"}, {"failed", "model_failed"}});
        CHECK(o.status == "partial");
        CHECK(o.stop_reason == "model_failed");
        CHECK(o.completed == 1);
        CHECK(o.failed == 1);
        CHECK(o.status != "done");
        CHECK(o.stop_reason != "built");
    }
    // All done → done/built.
    {
        auto o = finish({{"done", "built"}, {"done", "assembled"}});
        CHECK(o.status == "done");
        CHECK(o.stop_reason == "built");
        CHECK(o.failed == 0);
    }
    // All failed → failed (not partial).
    {
        auto o = finish({{"failed", "provider_error"}, {"failed", "model_failed"}});
        CHECK(o.status == "failed");
        CHECK(o.stop_reason == "provider_error");
        CHECK(o.completed == 0);
    }
}

TEST_CASE("execution_style inheritance for sub-runs") {
    // Mirrors run_design / run_assembly_design: options.execution_style wins; is_sub_run
    // must not re-derive edit from recommend_structure.
    auto resolve = [](Complexity cx, const std::string& mode, const std::string& opt_style,
                      bool is_sub_run) {
        ExecStructure rec = recommend_structure(cx, false);
        std::string style = execution_style_from_structure(structure_label(rec));
        if (mode == "swarm") style = "swarm";
        else if (mode == "eng_firm") style = "eng_firm";
        else if (mode == "edit") style = "edit";
        // options.execution_style explicit override (parent mission passes this through).
        if (opt_style == "edit" || opt_style == "swarm" || opt_style == "eng_firm")
            style = opt_style;
        // Nested units inherit; mode is erased so only opt_style carries the parent stamp.
        (void)is_sub_run;
        return style;
    };
    // Parent swarm → child with mode erased + execution_style=swarm keeps swarm.
    CHECK(resolve(Complexity::Solo, /*mode=*/"", /*opt_style=*/"swarm", true) == "swarm");
    CHECK(resolve(Complexity::Solo, "", "eng_firm", true) == "eng_firm");
    // Without inheritance a Solo child would collapse to edit — the bug.
    CHECK(resolve(Complexity::Solo, "", "", true) == "edit");
    // Top-level mode still forces style.
    CHECK(resolve(Complexity::Solo, "swarm", "", false) == "swarm");
    CHECK(resolve(Complexity::Complex, "edit", "", false) == "edit");
}

TEST_CASE("mode override forces execution_style over recommend_structure") {
    // Mirrors run.cpp: explicit swarm/eng_firm overrides recommendation.
    auto resolve = [](Complexity cx, const std::string& mode) {
        ExecStructure rec = recommend_structure(cx, false);
        std::string style = execution_style_from_structure(structure_label(rec));
        if (mode == "swarm") style = "swarm";
        else if (mode == "eng_firm") style = "eng_firm";
        else if (mode == "edit") style = "edit";
        return style;
    };
    // Solo would recommend edit, but mode=swarm forces swarm.
    CHECK(resolve(Complexity::Solo, "swarm") == "swarm");
    CHECK(resolve(Complexity::Solo, "eng_firm") == "eng_firm");
    CHECK(resolve(Complexity::Complex, "edit") == "edit");
    CHECK(resolve(Complexity::Solo, "") == "edit");
}

TEST_CASE("advisor fallback field values") {
    // Soft-approve keeps resilience; UI reads fallback for 'advisor unavailable'.
    auto v = parse_advisor_plan_verdict("");
    CHECK(v.fallback == "unparseable");
    CHECK(v.verdict == "approve");
    // Provider-error is stamped at the call site (not the parser).
    std::string fallback;
    bool has_call_error = true;
    if (has_call_error)
        fallback = "provider_error";
    else if (!v.ok)
        fallback = "unparseable";
    CHECK(fallback == "provider_error");
}

TEST_CASE("mission planner degraded stub flag") {
    // When planner returns empty tasks → single "Build the model" stub with degraded=true.
    // Distinguishes 403/provider-failure fallback from a real 1-task plan.
    auto is_degraded = [](bool planner_ok, size_t parsed_tasks) -> bool {
        return !planner_ok || parsed_tasks == 0;
    };
    auto task_count = [](bool planner_ok, size_t parsed_tasks) -> size_t {
        if (!planner_ok || parsed_tasks == 0) return 1;  // stub
        return parsed_tasks;
    };
    CHECK(is_degraded(false, 0));
    CHECK(task_count(false, 0) == 1);
    CHECK_FALSE(is_degraded(true, 3));
    CHECK(task_count(true, 3) == 3);
    // Real single-task plan from a successful planner is NOT degraded.
    CHECK_FALSE(is_degraded(true, 1));
    CHECK(task_count(true, 1) == 1);
}

// ---------------------------------------------------------------------------
// Interface-aware assembly planning (iface_plan)
// ---------------------------------------------------------------------------

namespace {

json iface_bolted_plan(const char* seed = "bracket_a", double dia = 5.5, int count = 2) {
    json holes = json::object();
    holes["count"] = count;
    holes["dia"] = dia;
    holes["pattern"] = "line";
    holes["spacing"] = 60;

    json iface = json::object();
    iface["between"] = json::array({"bracket_a", "bracket_b"});
    iface["kind"] = "bolted_joint";
    iface["seed"] = seed;
    iface["holes"] = holes;
    iface["fit"] = "clearance";
    iface["bolt"] = "M5";

    json plan = json::object();
    plan["interfaces"] = json::array({iface});
    return plan;
}

json hole_feature_tree(double diameter, int count) {
    json params = json::object();
    params["diameter"] = diameter;
    params["count"] = count;
    json feat = json::object();
    feat["id"] = "h1";
    feat["type"] = "hole";
    feat["params"] = params;
    json tree = json::object();
    tree["features"] = json::array({feat});
    return tree;
}

}  // namespace

TEST_CASE("interface ledger parses planner interfaces and seed") {
    json plan = iface_bolted_plan("bracket_a", 5.5, 2);
    plan["name"] = "back_to_back_brackets";
    plan["assembly"] = true;
    plan["parts"] = json::array({json{{"name", "bracket_b"}}, json{{"name", "bracket_a"}},
                                 json{{"name", "m5_bolt"}}});

    InterfaceLedger led = parse_interface_ledger(plan);
    REQUIRE(led.interfaces.size() == 1);
    CHECK(led.interfaces[0].kind == "bolted_joint");
    CHECK(led.interfaces[0].seed == "bracket_a");
    REQUIRE(led.interfaces[0].between.size() == 2);
    CHECK(led.interfaces[0].params.contains("holes"));
    const double dia = led.interfaces[0].params["holes"]["dia"].get<double>();
    CHECK(dia == doctest::Approx(5.5));
    // bracket_b depends on seed bracket_a
    REQUIRE(led.part_deps.count("bracket_b") == 1);
    REQUIRE_FALSE(led.part_deps["bracket_b"].empty());
    CHECK(led.part_deps["bracket_b"][0] == "bracket_a");
    CHECK(led.is_seed["bracket_a"]);
}

TEST_CASE("interface build order puts seed before dependent") {
    // Planner listed dependent first; interface order must still seed-first.
    std::vector<std::string> names = {"bracket_b", "bracket_a", "spacer"};
    InterfaceLedger led = parse_interface_ledger(iface_bolted_plan());
    std::vector<size_t> order = order_parts_by_interfaces(names, led);
    REQUIRE(order.size() == 3);
    size_t pos_a = 99, pos_b = 99, pos_s = 99;
    for (size_t i = 0; i < order.size(); ++i) {
        if (names[order[i]] == "bracket_a") pos_a = i;
        if (names[order[i]] == "bracket_b") pos_b = i;
        if (names[order[i]] == "spacer") pos_s = i;
    }
    CHECK(pos_a < pos_b);  // seed before dependent
    CHECK(pos_s != static_cast<size_t>(99));
}

TEST_CASE("interface deps fold into swarm waves — dependent later wave") {
    // Two part units with empty deps + one interface: after fold, dependent cannot share wave 0.
    json t1 = json::object();
    t1["id"] = "u1";
    t1["title"] = "L-bracket A (seed)";
    t1["request"] = "build bracket_a";
    t1["deps"] = json::array();
    json t2 = json::object();
    t2["id"] = "u2";
    t2["title"] = "L-bracket B";
    t2["request"] = "build bracket_b matching A";
    t2["deps"] = json::array();
    json t3 = json::object();
    t3["id"] = "u3";
    t3["title"] = "Assemble";
    t3["request"] = "assemble brackets";
    t3["deps"] = json::array({"u1", "u2"});
    json tasks = json::array({t1, t2, t3});

    InterfaceLedger led = parse_interface_ledger(iface_bolted_plan());
    json folded = fold_interface_deps_into_tasks(tasks, led);

    bool u2_deps_u1 = false;
    for (const auto& t : folded) {
        if (!t.is_object()) continue;
        if (t.value("id", std::string()) != "u2") continue;
        REQUIRE(t.contains("deps"));
        for (const auto& d : t["deps"]) {
            if (d.is_string() && d.get<std::string>() == "u1") u2_deps_u1 = true;
        }
    }
    CHECK(u2_deps_u1);

    auto waves = waves_from_task_deps(folded);
    REQUIRE(waves.size() >= 2);
    auto wave_of = [&](const std::string& id) -> int {
        for (size_t w = 0; w < waves.size(); ++w) {
            for (size_t idx : waves[w]) {
                if (folded[idx].value("id", std::string()) == id) return static_cast<int>(w);
            }
        }
        return -1;
    };
    const int w1 = wave_of("u1");
    const int w2 = wave_of("u2");
    const int w3 = wave_of("u3");
    CHECK(w1 >= 0);
    CHECK(w2 > w1);  // dependent later than seed — never same wave
    const bool asm_after = (w3 > w2) || (w3 > w1);
    CHECK(asm_after);
}

TEST_CASE("interface advisory injects measured mate for dependent") {
    InterfaceLedger led = parse_interface_ledger(iface_bolted_plan());

    json tree = hole_feature_tree(5.5, 2);
    json bbox = json::object();
    bbox["size"] = json::array({80.0, 40.0, 5.0});
    MeasuredInterface seed_m = measure_interface_from_tree("bracket_a", tree, bbox);
    std::map<std::string, MeasuredInterface> built;
    built["bracket_a"] = seed_m;

    const std::string dep_adv = build_interface_advisory("bracket_b", led, built);
    REQUIRE_FALSE(dep_adv.empty());
    const bool has_exists = dep_adv.find("ALREADY EXISTS") != std::string::npos;
    const bool has_exact = dep_adv.find("match them EXACTLY") != std::string::npos;
    const bool has_mate = dep_adv.find("bracket_a") != std::string::npos;
    const bool has_dia = dep_adv.find("5.5") != std::string::npos;
    CHECK(has_exists);
    CHECK(has_exact);
    CHECK(has_mate);
    CHECK(has_dia);

    // Seed advisory when mate not built: BINDING standard from plan.
    std::map<std::string, MeasuredInterface> empty_built;
    const std::string seed_adv = build_interface_advisory("bracket_a", led, empty_built);
    REQUIRE_FALSE(seed_adv.empty());
    const bool seed_label = seed_adv.find("you are the SEED") != std::string::npos;
    const bool seed_bind = seed_adv.find("BINDING") != std::string::npos;
    const bool seed_dia = seed_adv.find("5.5") != std::string::npos;
    CHECK(seed_label);
    CHECK(seed_bind);
    CHECK(seed_dia);
}

TEST_CASE("interface mismatch routes fix to dependent not seed") {
    InterfaceLedger led = parse_interface_ledger(iface_bolted_plan());
    json tree_seed = hole_feature_tree(5.5, 2);
    json tree_dep = hole_feature_tree(4.2, 2);
    std::map<std::string, MeasuredInterface> built;
    built["bracket_a"] = measure_interface_from_tree("bracket_a", tree_seed);
    built["bracket_b"] = measure_interface_from_tree("bracket_b", tree_dep);

    auto mms = verify_interfaces(led, built, /*dia_tol_mm=*/0.15);
    REQUIRE_FALSE(mms.empty());
    for (const auto& mm : mms) {
        CHECK(mm.dependent_part == "bracket_b");
        CHECK(mm.seed_part == "bracket_a");
        const bool dep_is_not_seed = mm.dependent_part != mm.seed_part;
        CHECK(dep_is_not_seed);
    }
    const std::string fix_dep = build_mismatch_fix_advisory(mms, "bracket_b");
    REQUIRE_FALSE(fix_dep.empty());
    const bool mentions_dep_role =
        fix_dep.find("DEPENDENT") != std::string::npos ||
        fix_dep.find("dependent") != std::string::npos;
    const bool mentions_part = fix_dep.find("bracket_b") != std::string::npos;
    const bool protects_seed =
        fix_dep.find("do NOT modify") != std::string::npos ||
        fix_dep.find("SEED") != std::string::npos;
    CHECK(mentions_dep_role);
    CHECK(mentions_part);
    CHECK(protects_seed);
    // Seed must not receive a fix advisory targeted at it as dependent.
    const std::string fix_seed = build_mismatch_fix_advisory(mms, "bracket_a");
    CHECK(fix_seed.empty());
}

TEST_CASE("interface advisory present in modeler context via advisory seam") {
    // Mirrors run_design: options.advisory_text → set_context_advisory → build_context body.
    clear_context_advisory();
    InterfaceLedger led = parse_interface_ledger(iface_bolted_plan());
    json tree = hole_feature_tree(5.5, 2);
    std::map<std::string, MeasuredInterface> built;
    built["bracket_a"] = measure_interface_from_tree("bracket_a", tree);
    const std::string adv = build_interface_advisory("bracket_b", led, built);
    REQUIRE_FALSE(adv.empty());
    set_context_advisory(adv);
    DesignState st;
    st.request = "Build bracket_b matching mate";
    st.complexity = Complexity::Standard;
    st.complexity_set = true;
    BuiltContext bc = build_context(st, "modeler", /*prompts_dir=*/"", /*context_cap=*/8000);
    const bool has_adv = bc.body.find("ADVISORY:") != std::string::npos;
    const bool has_exists = bc.body.find("ALREADY EXISTS") != std::string::npos;
    const bool has_exact = bc.body.find("match them EXACTLY") != std::string::npos;
    CHECK(has_adv);
    CHECK(has_exists);
    CHECK(has_exact);
    clear_context_advisory();
}

TEST_CASE("cancel_run cascades to mission sub-runs") {
    using namespace chudware::agent;
    RunControl parent, child, other;
    register_run_control("mission-1", parent);
    register_run_control("mission-1-t0", child);
    register_run_control("mission-2", other);
    json r = cancel_run("mission-1");
    CHECK(r.value("cancelled", false));
    CHECK(r.value("count", 0) == 2);
    CHECK(parent.flag->load());
    CHECK(child.flag->load());
    CHECK_FALSE(other.flag->load());
    // An unrelated run id that merely shares a word prefix (no dash) must NOT cancel.
    CHECK(cancel_run("missionX").value("cancelled", false) == false);
    unregister_run_control("mission-1");
    unregister_run_control("mission-1-t0");
    unregister_run_control("mission-2");
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

TEST_CASE("BuiltContext carries the prompt source telemetry used to re-load") {
    // agent_call / the reviewer stage used to call load_prompt() a SECOND time with
    // default flags purely to recover the source path for telemetry. build_context now
    // carries it. The source path depends only on (role, prompts_dir) + the override
    // file, never on the cheatsheet flags, so telemetry is byte-identical to the old
    // second load — and only one prompt assembly happens per model call.
    DesignState st;
    st.request = "make a 20mm cube";

    BuiltContext intake = build_context(st, "intake", /*prompts_dir=*/"", kContextTokenCap);
    CHECK_FALSE(intake.prompt_source.empty());
    CHECK(intake.prompt_source == load_prompt("intake", /*prompts_dir=*/"").second);

    // An ops role with Solo complexity loads the SLIM cheatsheet (different system text,
    // different load_prompt flags) yet reports the same source path telemetry recorded.
    st.complexity = Complexity::Solo;
    st.complexity_set = true;
    BuiltContext solo = build_context(st, "modeler", /*prompts_dir=*/"", kContextTokenCap);
    CHECK(solo.prompt_source == load_prompt("modeler", /*prompts_dir=*/"").second);

    st.complexity = Complexity::Standard;
    BuiltContext full = build_context(st, "modeler", /*prompts_dir=*/"", kContextTokenCap);
    CHECK(full.prompt_source == solo.prompt_source);
    // Guard the premise: the two systems really are different assemblies of one source.
    CHECK(full.system != solo.system);
}
