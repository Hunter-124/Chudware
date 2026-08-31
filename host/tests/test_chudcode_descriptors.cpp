// test_chudcode_descriptors.cpp — the Chudcode worker payload parsers behind Settings →
// Providers, the copilot model picker/cost strip, the Tools built-in rows, and the
// /compact chat-replacement guard. Links the REAL chudcode_descriptors.cpp (GL-free), so
// these are the shipped rules, not copies.
//
// The rules under test:
//   * provider descriptors accept BOTH the frozen spelling (label/state/authKinds) and the
//     Chudcode-native one (name + authenticated/method); an entry without an id is rejected
//     rather than silently invented;
//   * usable = whitelist (key_set/oauth_connected), never "anything but signed_out";
//   * a streamed login frame carrying a URL under any spelling IS an open_url hand-off;
//   * session stats read flat or nested spellings and report hasCost only when a dollar
//     figure was actually present (0 is a real value);
//   * a compact result may replace the visible chat ONLY with a nonempty explicit payload —
//     Chudcode online compaction omits messages, and an empty array must never wipe chat.
#include <cassert>
#include <cstdio>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "ui/chudcode_descriptors.h"

using nlohmann::json;

namespace {

void test_provider_desc_frozen_spelling() {
    const json p = {
        {"id", "anthropic"}, {"label", "Anthropic"}, {"state", "oauth_connected"},
        {"origin", "oauth"}, {"consoleUrl", "https://console.anthropic.com"},
        {"modelsAvailable", 12}, {"authKinds", json::array({"api_key", "oauth"})},
    };
    ui::ChudcodeProviderDesc d;
    assert(ui::parseChudcodeProviderDesc(p, d));
    assert(d.id == "anthropic");
    assert(d.label == "Anthropic");
    assert(d.state == "oauth_connected");
    assert(d.hasApiKey && d.hasOAuth);
    assert(d.modelsAvailable == 12);
    assert(d.consoleUrl == "https://console.anthropic.com");
}

void test_provider_desc_native_spelling_derives_state() {
    // Chudcode-native: name + authenticated + method, no state string.
    ui::ChudcodeProviderDesc oauth;
    assert(ui::parseChudcodeProviderDesc(
        {{"id", "kimi-code"}, {"name", "Kimi Code"}, {"authenticated", true},
         {"method", "oauth"}},
        oauth));
    assert(oauth.label == "Kimi Code");
    assert(oauth.state == "oauth_connected");
    assert(oauth.hasOAuth && !oauth.hasApiKey);   // scalar method names ONE capability

    ui::ChudcodeProviderDesc key;
    assert(ui::parseChudcodeProviderDesc(
        {{"id", "openai"}, {"authenticated", true}, {"method", "api"}}, key));
    assert(key.state == "key_set");
    assert(key.hasApiKey && !key.hasOAuth);

    ui::ChudcodeProviderDesc out;
    assert(ui::parseChudcodeProviderDesc({{"id", "xai"}, {"authenticated", false}}, out));
    assert(out.state == "signed_out");
    assert(out.label == "xai");   // label defaults to id, never blank
}

void test_provider_desc_rejects_unkeyed_entry() {
    ui::ChudcodeProviderDesc d;
    d.id = "sentinel";
    assert(!ui::parseChudcodeProviderDesc(json{{"label", "no id"}}, d));
    assert(!ui::parseChudcodeProviderDesc(json("not an object"), d));
    assert(d.id == "sentinel");   // out untouched on rejection
}

void test_usable_is_a_whitelist() {
    assert(ui::chudcodeProviderUsable("key_set"));
    assert(ui::chudcodeProviderUsable("oauth_connected"));
    // A revoked credential ("error") fails every request; "disabled" likewise. Treating
    // either as signed-in would advertise a provider that cannot serve a single call.
    assert(!ui::chudcodeProviderUsable("error"));
    assert(!ui::chudcodeProviderUsable("disabled"));
    assert(!ui::chudcodeProviderUsable("signed_out"));
    assert(!ui::chudcodeProviderUsable(""));
}

void test_login_event_url_wins_regardless_of_tag() {
    // Tagged open_url.
    ui::ChudcodeLoginEvent e;
    assert(ui::parseChudcodeLoginEvent(
        {{"kind", "open_url"}, {"url", "https://a.example/auth"}}, e));
    assert(e.kind == "open_url" && e.url == "https://a.example/auth");

    // extension_ui_request carrying a URL is still a browser hand-off.
    assert(ui::parseChudcodeLoginEvent(
        {{"type", "extension_ui_request"}, {"url", "https://b.example/device"},
         {"instructions", "Approve in browser"}},
        e));
    assert(e.kind == "open_url");
    assert(e.url == "https://b.example/device");
    assert(e.message == "Approve in browser");

    // Nested transport shape {"data": {...}} unwraps one level.
    assert(ui::parseChudcodeLoginEvent(
        {{"data", {{"kind", "open_url"}, {"url", "https://c.example"}}}}, e));
    assert(e.url == "https://c.example");

    // Progress-only frame: no URL, still deliverable text.
    assert(ui::parseChudcodeLoginEvent({{"kind", "progress"},
                                        {"message", "waiting for approval"}}, e));
    assert(e.kind == "progress" && e.url.empty() && e.message == "waiting for approval");

    // A frame with nothing usable is rejected, not invented.
    assert(!ui::parseChudcodeLoginEvent(json::object(), e));
    assert(!ui::parseChudcodeLoginEvent(json("x"), e));
}

void test_session_stats_nested_and_flat() {
    // Nested "stats" body with camelCase cache keys and a model row.
    ui::ChudcodeSessionStats s;
    assert(ui::parseChudcodeSessionStats(
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
    ui::ChudcodeSessionStats flat;
    assert(ui::parseChudcodeSessionStats(
        {{"ok", true}, {"tokens", {{"input", 7}, {"output", 3}}}}, flat));
    assert(!flat.hasCost && flat.tokensIn == 7 && flat.tokensOut == 3);

    // cost: 0 is a REAL dollar figure, not absence.
    ui::ChudcodeSessionStats zero;
    assert(ui::parseChudcodeSessionStats({{"ok", true}, {"cost", 0.0}}, zero));
    assert(zero.hasCost && zero.usd == 0.0);

    // ok:false is a refusal — out untouched.
    ui::ChudcodeSessionStats keep;
    keep.usd = 9.0;
    keep.hasCost = true;
    assert(!ui::parseChudcodeSessionStats({{"ok", false}, {"reason", "no session"}}, keep));
    assert(keep.hasCost && keep.usd == 9.0);
}

void test_tool_state_rows() {
    std::vector<ui::ChudcodeToolRow> rows;
    assert(ui::parseChudcodeToolState(
        {{"ok", true},
         {"tools", json::array({
              {{"name", "browser"}, {"loadMode", "lazy"}, {"builtin", true}},
              {{"name", "computer"}, {"load", "eager"}, {"available", false}},
              {{"description", "nameless — dropped"}},
          })}},
        rows));
    assert(rows.size() == 2);   // the nameless entry is dropped, not invented
    assert(rows[0].name == "browser" && rows[0].loadMode == "lazy" && rows[0].available);
    assert(rows[1].name == "computer" && rows[1].loadMode == "eager" && !rows[1].available);

    // No tools array / refusal → false so the panel keeps its last good rows.
    assert(!ui::parseChudcodeToolState({{"ok", true}}, rows));
    assert(!ui::parseChudcodeToolState({{"ok", false}, {"tools", json::array()}}, rows));
}

void test_stt_contract_and_transcript_insertion() {
    assert(std::string(ui::kChudcodeSttStatusMethod) == "chudcode_stt_status");
    assert(std::string(ui::kChudcodeSttStartMethod) == "chudcode_stt_start");
    assert(std::string(ui::kChudcodeSttStopMethod) == "chudcode_stt_stop");
    assert(std::string(ui::kChudcodeSttCancelMethod) == "chudcode_stt_cancel");

    ui::ChudcodeSttResult status;
    assert(ui::parseChudcodeSttResult(
        {{"ok", true}, {"available", true}, {"state", "transcribing"},
         {"model", "whisper-small"}, {"progress", 135}},
        status));
    assert(status.available && status.state == "transcribing");
    assert(status.model == "whisper-small" && status.progress == 100);

    assert(ui::parseChudcodeSttResult(
        {{"ok", true}, {"available", false}, {"state", "idle"},
         {"error", "microphone unavailable"}},
        status));
    assert(!status.available && status.error == "microphone unavailable");

    assert(ui::parseChudcodeSttResult(
        {{"ok", true}, {"available", true}, {"state", "idle"},
         {"text", "make the bore wider"}},
        status));
    assert(status.text == "make the bore wider");
    assert(ui::composeChudcodeTranscript("Existing prompt", status.text, "") ==
           "Existing prompt make the bore wider");
    assert(ui::composeChudcodeTranscript("Existing prompt\n", "committed", "partial") ==
           "Existing prompt\ncommitted partial");

    ui::ChudcodeSttResult untouched;
    untouched.state = "sentinel";
    assert(!ui::parseChudcodeSttResult(
        {{"ok", true}, {"available", true}, {"state", "listening"}}, untouched));
    assert(!ui::parseChudcodeSttResult(
        {{"ok", false}, {"reason", "session_not_live"}}, untouched));
    assert(untouched.state == "sentinel");
}

void test_onboarding_key_acknowledgements_are_provider_bound() {
    assert(std::string(ui::kChudcodeValidateKeyMethod) == "chudcode_validate_key");
    assert(std::string(ui::kChudcodeSetKeyMethod) == "chudcode_set_key");
    assert(ui::chudcodeKeyValidationAccepted(
        {{"ok", true}, {"providerId", "openai"}, {"valid", true}}, "openai"));
    assert(!ui::chudcodeKeyValidationAccepted(
        {{"ok", true}, {"providerId", "openai"}, {"valid", false}}, "openai"));
    assert(!ui::chudcodeKeyValidationAccepted(
        {{"ok", true}, {"providerId", "anthropic"}, {"valid", true},
         {"apiKey", "must-never-matter"}},
        "openai"));
    assert(!ui::chudcodeKeyValidationAccepted(
        {{"ok", false}, {"providerId", "openai"}, {"valid", true},
         {"reason", "rejected"}},
        "openai"));

    const json saved = {
        {"ok", true},
        {"provider",
         {{"providerId", "openai"}, {"authenticated", true}, {"hasApiKey", true}}},
    };
    assert(ui::chudcodeKeySaveAccepted(saved, "openai"));
    assert(!ui::chudcodeKeySaveAccepted(saved, "anthropic"));
    assert(!ui::chudcodeKeySaveAccepted(
        {{"ok", true},
         {"provider",
          {{"providerId", "openai"}, {"authenticated", false}, {"hasApiKey", true}}}},
        "openai"));
}

void test_compact_replacement_guard() {
    // The live defect this guards: Chudcode online compaction rewrites the service-side
    // session and returns NO messages — the visible chat must stay intact.
    assert(ui::chudcodeCompactReplacement({{"ok", true}, {"summary", "…"}}) == nullptr);
    // A present-but-EMPTY array is a degenerate payload, not "wipe the chat".
    assert(ui::chudcodeCompactReplacement({{"ok", true},
                                           {"messages", json::array()}}) == nullptr);
    assert(ui::chudcodeCompactReplacement({{"ok", true},
                                           {"kept_messages", json::array()}}) == nullptr);
    // Nonempty rebuilt conversation wins outright…
    const json rebuilt = {{"ok", true},
                          {"messages", json::array({{{"role", "user"}, {"text", "hi"}}})},
                          {"kept_messages", json::array({{{"role", "user"}}})}};
    const json* repl = ui::chudcodeCompactReplacement(rebuilt);
    assert(repl && repl == &rebuilt.at("messages"));
    // …and the legacy bare kept tail is the fallback.
    const json legacy = {{"ok", true},
                         {"kept_messages", json::array({{{"role", "user"}, {"text", "t"}}})}};
    repl = ui::chudcodeCompactReplacement(legacy);
    assert(repl && repl == &legacy.at("kept_messages"));
    // Non-object results never replace anything.
    assert(ui::chudcodeCompactReplacement(json("nope")) == nullptr);
}

}  // namespace

int main() {
    test_provider_desc_frozen_spelling();
    test_provider_desc_native_spelling_derives_state();
    test_provider_desc_rejects_unkeyed_entry();
    test_usable_is_a_whitelist();
    test_login_event_url_wins_regardless_of_tag();
    test_session_stats_nested_and_flat();
    test_tool_state_rows();
    test_compact_replacement_guard();
    test_stt_contract_and_transcript_insertion();
    test_onboarding_key_acknowledgements_are_provider_bound();
    std::printf("test_chudcode_descriptors: OK\n");
    return 0;
}
