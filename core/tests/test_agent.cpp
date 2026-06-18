// test_agent.cpp — unit tests for the agent provider layer (no network).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "agent/providers.h"

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

TEST_CASE("anthropic body shape") {
    json messages = json::array();
    messages.push_back(user_message(json::array({text_part("hello")})));
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
}

TEST_CASE("openai message text-join branch") {
    json parts = json::array({text_part("a"), text_part("b")});
    json m = openai_message("user", parts);
    CHECK(m["content"] == "a\n\nb");
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
