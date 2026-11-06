// test_omp_descriptors.cpp — the Omp worker payload parsers behind Settings →
// Providers, the copilot model picker/cost strip, and the
// /compact chat-replacement guard. Links the REAL omp_descriptors.cpp (GL-free), so
// these are the shipped rules, not copies.
//
// The rules under test:
//   * provider descriptors accept BOTH the frozen spelling (label/state/authKinds) and the
//     Omp-native one (name + authenticated/method); an entry without an id is rejected
//     rather than silently invented;
//   * usable = whitelist (key_set/oauth_connected), never "anything but signed_out";
//   * a streamed login frame carrying a URL under any spelling IS an open_url hand-off;
//   * session stats read flat or nested spellings and report hasCost only when a dollar
//     figure was actually present (0 is a real value);
//   * a compact result may replace the visible chat ONLY with a nonempty explicit payload —
//     Omp online compaction omits messages, and an empty array must never wipe chat.
#include <cassert>
#include <cstdio>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "ui/omp_descriptors.h"

using nlohmann::json;

namespace {

void test_provider_desc_frozen_spelling() {
    const json p = {
        {"id", "anthropic"}, {"label", "Anthropic"}, {"state", "oauth_connected"},
        {"origin", "oauth"}, {"consoleUrl", "https://console.anthropic.com"},
        {"modelsAvailable", 12}, {"authKinds", json::array({"api_key", "oauth"})},
    };
    ui::OmpProviderDesc d;
    assert(ui::parseOmpProviderDesc(p, d));
    assert(d.id == "anthropic");
    assert(d.label == "Anthropic");
    assert(d.state == "oauth_connected");
    assert(d.hasApiKey && d.hasOAuth);
    assert(d.modelsAvailable == 12);
    assert(d.consoleUrl == "https://console.anthropic.com");
}

void test_provider_desc_native_spelling_derives_state() {
    // Omp-native: name + authenticated + method, no state string.
    ui::OmpProviderDesc oauth;
    assert(ui::parseOmpProviderDesc(
        {{"id", "kimi-code"}, {"name", "Kimi Code"}, {"authenticated", true},
         {"method", "oauth"}},
        oauth));
    assert(oauth.label == "Kimi Code");
    assert(oauth.state == "oauth_connected");
    assert(oauth.hasOAuth && !oauth.hasApiKey);   // scalar method names ONE capability

    ui::OmpProviderDesc key;
    assert(ui::parseOmpProviderDesc(
        {{"id", "openai"}, {"authenticated", true}, {"method", "api"}}, key));
    assert(key.state == "key_set");
    assert(key.hasApiKey && !key.hasOAuth);

    ui::OmpProviderDesc out;
    assert(ui::parseOmpProviderDesc({{"id", "xai"}, {"authenticated", false}}, out));
    assert(out.state == "signed_out");
    assert(out.label == "xai");   // label defaults to id, never blank
    ui::OmpProviderDesc env;
    assert(ui::parseOmpProviderDesc(
        {{"id", "custom"}, {"authenticated", true}, {"origin", "env"},
         {"available", true}, {"modelsAvailable", 2}}, env));
    assert(env.state == "configured" && env.available && env.modelsAvailable == 2);
    assert(!env.hasApiKey && !env.hasOAuth);
}

void test_provider_desc_rejects_unkeyed_entry() {
    ui::OmpProviderDesc d;
    d.id = "sentinel";
    assert(!ui::parseOmpProviderDesc(json{{"label", "no id"}}, d));
    assert(!ui::parseOmpProviderDesc(json("not an object"), d));
    assert(d.id == "sentinel");   // out untouched on rejection
}

void test_availability_is_independent_of_credentials() {
    ui::OmpProviderDesc row;
    assert(ui::parseOmpProviderDesc({{"id", "custom"}, {"state", "signed_out"},
                                     {"available", true}}, row));
    assert(row.availabilityKnown && row.available && !row.hasApiKey && !row.hasOAuth);
    assert(ui::ompProviderAvailableAfterRefresh(row, false));
    assert(ui::ompProviderAvailableAfterRefresh(row, true));
    assert(ui::parseOmpProviderDesc({{"id", "custom"}, {"state", "unknown"},
                                     {"modelsAvailable", 4}}, row));
    assert(!row.availabilityKnown && !row.available && !row.hasApiKey);
    assert(!ui::ompProviderAvailableAfterRefresh(row, false));
    assert(ui::ompProviderAvailableAfterRefresh(row, true));
    assert(ui::parseOmpProviderDesc({{"id", "custom"}, {"available", false},
                                     {"state", "key_set"}}, row));
    assert(row.availabilityKnown && !row.available && row.state == "key_set");
    assert(!ui::ompProviderAvailableAfterRefresh(row, false));
    assert(!ui::ompProviderAvailableAfterRefresh(row, true));
    for (const char* state : {"signed_out", "error", "disabled"}) {
        assert(ui::parseOmpProviderDesc({{"id", "custom"}, {"state", state}}, row));
        assert(!ui::ompProviderAvailableAfterRefresh(row, true));
    }
}

void test_usable_is_a_whitelist() {
    assert(ui::ompProviderUsable("key_set"));
    assert(ui::ompProviderUsable("oauth_connected"));
    assert(ui::ompProviderUsable("configured"));
    // Refusal states do not describe configured authentication.
    assert(!ui::ompProviderUsable("error"));
    assert(!ui::ompProviderUsable("disabled"));
    assert(!ui::ompProviderUsable("signed_out"));
    assert(!ui::ompProviderUsable(""));
}

void test_login_event_url_wins_regardless_of_tag() {
    // Tagged open_url.
    ui::OmpLoginEvent e;
    assert(ui::parseOmpLoginEvent(
        {{"kind", "open_url"}, {"url", "https://a.example/auth"}}, e));
    assert(e.kind == "open_url" && e.url == "https://a.example/auth");

    // extension_ui_request carrying a URL is still a browser hand-off.
    assert(ui::parseOmpLoginEvent(
        {{"type", "extension_ui_request"}, {"url", "https://b.example/device"},
         {"instructions", "Approve in browser"}},
        e));
    assert(e.kind == "open_url");
    assert(e.url == "https://b.example/device");
    assert(e.message == "Approve in browser");

    // Nested transport shape {"data": {...}} unwraps one level.
    assert(ui::parseOmpLoginEvent(
        {{"data", {{"kind", "open_url"}, {"url", "https://c.example"}}}}, e));
    assert(e.url == "https://c.example");

    // Progress-only frame: no URL, still deliverable text.
    assert(ui::parseOmpLoginEvent({{"kind", "progress"},
                                        {"message", "waiting for approval"}}, e));
    assert(e.kind == "progress" && e.url.empty() && e.message == "waiting for approval");

    // A frame with nothing usable is rejected, not invented.
    assert(!ui::parseOmpLoginEvent(json::object(), e));
    assert(!ui::parseOmpLoginEvent(json("x"), e));
}

void test_session_stats_nested_and_flat() {
    // Nested "stats" body with camelCase cache keys and a model row.
    ui::OmpSessionStats s;
    assert(ui::parseOmpSessionStats(
        {{"ok", true},
         {"stats",
          {{"cost", 1.25},
           {"tokens", {{"input", 1000}, {"output", 250}, {"cacheRead", 90}}},
           {"models", json::array({{{"model", "m1"}, {"provider", "anthropic"},
                                    {"usd", 1.25},
                                    {"tokens", {{"input", 1000}, {"output", 250}}}}})}}}},
        s));
    assert(s.hasCost && s.usd == 1.25);
    assert(s.tokensIn == 1000 && s.tokensOut == 250 && s.cacheRead == 90);
    assert(s.models.size() == 1);
    assert(s.models[0].model == "m1" && s.models[0].priced);

    // Flat body, no cost reported: hasCost must be FALSE (0 would be a fabricated figure),
    // tokens still land.
    ui::OmpSessionStats flat;
    assert(ui::parseOmpSessionStats(
        {{"ok", true}, {"tokens", {{"input", 7}, {"output", 3}}}}, flat));
    assert(!flat.hasCost && flat.tokensIn == 7 && flat.tokensOut == 3);

    // cost: 0 is a REAL dollar figure, not absence.
    ui::OmpSessionStats zero;
    assert(ui::parseOmpSessionStats({{"ok", true}, {"cost", 0.0}}, zero));
    assert(zero.hasCost && zero.usd == 0.0);

    // ok:false is a refusal — out untouched.
    ui::OmpSessionStats keep;
    keep.usd = 9.0;
    keep.hasCost = true;
    assert(!ui::parseOmpSessionStats({{"ok", false}, {"reason", "no session"}}, keep));
    assert(keep.hasCost && keep.usd == 9.0);
}


void test_tool_state_uses_only_reported_metadata() {
    std::vector<ui::OmpToolRow> rows;
    assert(ui::parseOmpToolState({{"ok", true}, {"tools", json::array({
        {{"name", "browser"}, {"builtin", true}, {"available", true}},
        {{"name", "unknown"}},
        {{"description", "missing name"}}
    })}}, rows));
    assert(rows.size() == 2);
    assert(rows[0].builtin && rows[0].availabilityKnown && rows[0].available);
    assert(!rows[1].availabilityKnown && !rows[1].available);
    assert(!ui::parseOmpToolState({{"ok", false}, {"tools", json::array()}}, rows));
}

void test_onboarding_key_acknowledgements_are_provider_bound() {
    assert(std::string(ui::kOmpSetKeyMethod) == "omp_set_key");

    const json saved = {
        {"ok", true},
        {"provider",
         {{"providerId", "openai"}, {"authenticated", true}, {"hasApiKey", true}}},
    };
    assert(ui::ompKeySaveAccepted(saved, "openai"));
    assert(!ui::ompKeySaveAccepted(saved, "anthropic"));
    assert(!ui::ompKeySaveAccepted(
        {{"ok", true},
         {"provider",
          {{"providerId", "openai"}, {"authenticated", false}, {"hasApiKey", true}}}},
        "openai"));
}

void test_compact_replacement_guard() {
    // The live defect this guards: Omp online compaction rewrites the service-side
    // session and returns NO messages — the visible chat must stay intact.
    assert(ui::ompCompactReplacement({{"ok", true}, {"summary", "…"}}) == nullptr);
    // A present-but-EMPTY array is a degenerate payload, not "wipe the chat".
    assert(ui::ompCompactReplacement({{"ok", true},
                                           {"messages", json::array()}}) == nullptr);
    assert(ui::ompCompactReplacement({{"ok", true},
                                           {"kept_messages", json::array()}}) == nullptr);
    // Nonempty rebuilt conversation wins outright…
    const json rebuilt = {{"ok", true},
                          {"messages", json::array({{{"role", "user"}, {"text", "hi"}}})},
                          {"kept_messages", json::array({{{"role", "user"}}})}};
    const json* repl = ui::ompCompactReplacement(rebuilt);
    assert(repl && repl == &rebuilt.at("messages"));
    // …and the legacy bare kept tail is the fallback.
    const json legacy = {{"ok", true},
                         {"kept_messages", json::array({{{"role", "user"}, {"text", "t"}}})}};
    repl = ui::ompCompactReplacement(legacy);
    assert(repl && repl == &legacy.at("kept_messages"));
    // Non-object results never replace anything.
    assert(ui::ompCompactReplacement(json("nope")) == nullptr);
}

}  // namespace

int main() {
    test_provider_desc_frozen_spelling();
    test_provider_desc_native_spelling_derives_state();
    test_provider_desc_rejects_unkeyed_entry();
    test_usable_is_a_whitelist();
    test_availability_is_independent_of_credentials();
    test_login_event_url_wins_regardless_of_tag();
    test_session_stats_nested_and_flat();
    test_tool_state_uses_only_reported_metadata();
    test_compact_replacement_guard();
    test_onboarding_key_acknowledgements_are_provider_bound();
    std::printf("test_omp_descriptors: OK\n");
    return 0;
}
