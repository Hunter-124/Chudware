// test_omp_session_actions.cpp — the Omp session-recovery seam behind the copilot
// Sessions panel (Manage card) and the conversation store's "omp" link. Links the REAL
// omp_session_actions.cpp (GL-free), so these are the shipped rules, not copies.
//
// The rules under test:
//   * supported method names and request params;
//   * parsers reject refusals and cancelled/empty branches instead of inventing success;
//   * "session_not_live" is a CLEAN refusal (resume hint), distinct from method absence
//     (surface latched off) and from transport errors;
//   * the per-conversation link round-trips id/file/health ONLY — persist adds exactly the
//     "omp" key and never touches the transcript ("msgs"), so no transcript is ever
//     duplicated or lost; runtimeLive is process-local and never serialized, which is what
//     makes every session INACTIVE after an app restart until a turn resumes it;
//   * re-keying a duplicate session id carries the link with the transcript it describes.
#include <cassert>
#include <cstdio>
#include <string>

#include <nlohmann/json.hpp>

#include "ui/omp_session_actions.h"

using nlohmann::json;

namespace {

// ---- request params ---------------------------------------------------------------------------

void test_method_names_are_contract_frozen() {
    assert(std::string(ui::kOmpExportSessionMethod) == "omp_export_session");
    assert(std::string(ui::kOmpBranchSessionMethod) == "omp_branch_session");
    assert(std::string(ui::kOmpCloseSessionMethod) == "omp_close_session");
}

void test_export_params_omit_empty_output_path() {
    // The worker guards unknown/odd params — a default export must send session_id ONLY.
    const json d = ui::ompExportSessionParams("c7");
    assert(d.size() == 1 && d.at("session_id") == "c7");
    const json p = ui::ompExportSessionParams("c7", "/tmp/out.html");
    assert(p.size() == 2 && p.at("output_path") == "/tmp/out.html");
}

void test_branch_close_params() {
    const json b = ui::ompBranchSessionParams("c7", "entry-42");
    assert(b.size() == 2 && b.at("session_id") == "c7" && b.at("entry_id") == "entry-42");
    const json cl = ui::ompCloseSessionParams("c7");
    assert(cl.size() == 1 && cl.at("session_id") == "c7");
}

// ---- refusal / absence triage -----------------------------------------------------------------

void test_action_error_prefers_reason_then_error() {
    assert(ui::ompActionError({{"ok", false}, {"reason", "nope"}}, "fb") == "nope");
    assert(ui::ompActionError({{"ok", false}, {"error", "bad"}}, "fb") == "bad");
    assert(ui::ompActionError(json("not an object"), "fb") == "fb");
}

void test_session_not_live_is_a_clean_refusal() {
    assert(ui::ompSessionNotLive({{"ok", false}, {"reason", "session_not_live"}}));
    // Never a false positive on success or on an unrelated failure.
    assert(!ui::ompSessionNotLive({{"ok", true}}));
    assert(!ui::ompSessionNotLive({{"ok", false}, {"reason", "export failed"}}));
    assert(!ui::ompSessionNotLive(json("x")));
}

void test_rpc_absent_latch_conditions() {
    // Transport-level unknown method (older worker).
    assert(ui::ompSessionRpcAbsent(false, "unknown method omp_export_session", {}));
    // Envelope-level absence spellings.
    assert(ui::ompSessionRpcAbsent(true, "", {{"ok", false}, {"reason", "unknown_method"}}));
    assert(ui::ompSessionRpcAbsent(true, "", {{"code", "method_not_found"}}));
    // A live method refusing for a REAL reason must NOT latch the surface off.
    assert(!ui::ompSessionRpcAbsent(true, "", {{"ok", false}, {"reason", "session_not_live"}}));
    assert(!ui::ompSessionRpcAbsent(false, "timeout", {}));
}

// ---- export -----------------------------------------------------------------------------------

void test_export_parses_path_and_rejects_refusal() {
    ui::OmpExportResult out;
    assert(ui::parseOmpExportResult(
        {{"ok", true}, {"path", "/proj/chat-exports/c7.html"}}, out));
    assert(out.path == "/proj/chat-exports/c7.html");
    // Refusal, missing path, and non-object are all failures — out untouched.
    out.path = "sentinel";
    assert(!ui::parseOmpExportResult({{"ok", false}, {"reason", "session_not_live"}}, out));
    assert(!ui::parseOmpExportResult({{"ok", true}}, out));
    assert(!ui::parseOmpExportResult(json("x"), out));
    assert(out.path == "sentinel");
}

// ---- branch -----------------------------------------------------------------------------------

void test_branch_requires_both_child_keys() {
    ui::OmpBranchResult out;
    assert(ui::parseOmpBranchResult({{"ok", true},
                                          {"omp_session_id", "cc-9"},
                                          {"omp_session_file", "/s/cc-9.jsonl"}},
                                         out));
    assert(out.sessionId == "cc-9" && out.sessionFile == "/s/cc-9.jsonl");
    // A branch without a resume file (or id) would mint a dead fork — reject it.
    out.sessionId = "sentinel";
    assert(!ui::parseOmpBranchResult({{"ok", true}, {"omp_session_id", "cc-9"}}, out));
    assert(!ui::parseOmpBranchResult(
        {{"ok", true}, {"omp_session_file", "/s/x.jsonl"}}, out));
    assert(!ui::parseOmpBranchResult({{"ok", false}, {"reason", "cancelled"}}, out));
    assert(out.sessionId == "sentinel");
}

void test_close_is_ok_envelope_only() {
    assert(ui::parseOmpCloseResult({{"ok", true}}));                 // idempotent success
    assert(!ui::parseOmpCloseResult({{"ok", false}, {"reason", "x"}}));
    assert(!ui::parseOmpCloseResult(json("x")));
}

// ---- link registry + conversation-JSON persistence --------------------------------------------

void test_link_json_round_trip_and_empty_elision() {
    ui::OmpSessionLink link;
    link.sessionId = "cc-5";
    link.sessionFile = "/s/cc-5.jsonl";
    link.health = "healthy";
    link.runtimeLive = true;  // process-local — must NOT serialize
    const json j = ui::ompSessionLinkToJson(link);
    assert(j.is_object());
    assert(j.at("session_id") == "cc-5" && j.at("session_file") == "/s/cc-5.jsonl" &&
           j.at("health") == "healthy");
    assert(!j.contains("runtimeLive") && !j.contains("runtime_live"));

    ui::OmpSessionLink back;
    back.runtimeLive = true;  // pre-set to prove parse does not resurrect liveness…
    back.runtimeLive = false;
    assert(ui::ompSessionLinkFromJson(j, back));
    assert(back.sessionId == "cc-5" && back.sessionFile == "/s/cc-5.jsonl" &&
           back.health == "healthy");
    assert(!back.runtimeLive);  // …a restored session is INACTIVE until a turn resumes it

    // Empty link -> null (persist skips the key entirely); junk -> rejected.
    assert(ui::ompSessionLinkToJson(ui::OmpSessionLink{}).is_null());
    ui::OmpSessionLink untouched;
    untouched.sessionId = "sentinel";
    assert(!ui::ompSessionLinkFromJson(json::object(), untouched));
    assert(!ui::ompSessionLinkFromJson(json("x"), untouched));
    assert(untouched.sessionId == "sentinel");
}

void test_persist_adds_only_the_omp_key() {
    ui::ompSessionLinksClear();
    ui::OmpSessionLink link;
    link.sessionId = "cc-8";
    link.sessionFile = "/s/cc-8.jsonl";
    ui::ompSessionLinkSet("c8", link);

    // A realistic conversation object: the transcript must come through byte-identical —
    // the link is metadata BESIDE the messages, never a second copy of them.
    json convObj = {{"id", "c8"},
                    {"title", "Bracket"},
                    {"msgs", json::array({{{"u", true}, {"t", "make a bracket"}},
                                          {{"u", false}, {"t", "done"}}})}};
    const json msgsBefore = convObj["msgs"];
    ui::ompLinkPersist("c8", convObj);
    assert(convObj.contains("omp"));
    assert(convObj["omp"].at("session_file") == "/s/cc-8.jsonl");
    assert(convObj["msgs"] == msgsBefore);   // no transcript loss, no duplication
    assert(convObj.size() == 4);             // id + title + msgs + omp, nothing else

    // A conversation with no link gains NO key at all.
    json bare = {{"id", "c9"}, {"msgs", json::array()}};
    ui::ompLinkPersist("c9", bare);
    assert(!bare.contains("omp"));

    // Restore round-trip into a clean registry (the load path).
    ui::ompSessionLinksClear();
    assert(ui::ompSessionLink("c8") == nullptr);
    ui::ompLinkRestore("c8", convObj);
    const ui::OmpSessionLink* got = ui::ompSessionLink("c8");
    assert(got && got->sessionId == "cc-8" && got->sessionFile == "/s/cc-8.jsonl");
    assert(!got->runtimeLive);  // restart semantics: inactive until resumed
    ui::ompSessionLinksClear();
}

void test_legacy_link_migrates_without_rewriting_history() {
    ui::ompSessionLinksClear();
    const std::string original = "/old/runtime/session with spaces.jsonl";
    json conversation = {{"id", "legacy"}, {"chudcode", {{"session_id", "old-id"},
                          {"session_file", original}, {"health", "issues"}}},
                         {"msgs", json::array({{{"u", true}, {"t", "visible only"}}})}};
    const json messages = conversation["msgs"];
    ui::ompLinkRestore("legacy", conversation);
    const auto* link = ui::ompSessionLink("legacy");
    assert(link && link->sessionId == "old-id" && link->sessionFile == original);
    assert(!link->runtimeLive);
    ui::ompLinkPersist("legacy", conversation);
    assert(!conversation.contains("chudcode"));
    assert(conversation["omp"]["session_file"] == original);
    assert(conversation["msgs"] == messages);
    ui::ompSessionLinksClear();
    ui::ompLinkRestore("legacy", conversation);
    assert(ui::ompSessionLink("legacy")->sessionFile == original);
    ui::ompSessionLinksClear();
}

void test_registry_rekey_erase_and_ensure() {
    ui::ompSessionLinksClear();
    ui::OmpSessionLink link;
    link.sessionFile = "/s/cc-2.jsonl";
    ui::ompSessionLinkSet("c2", link);

    // persistChatHistory re-keys a duplicate id — the link follows the transcript.
    ui::ompSessionLinkRekey("c2", "c12");
    assert(ui::ompSessionLink("c2") == nullptr);
    const ui::OmpSessionLink* moved = ui::ompSessionLink("c12");
    assert(moved && moved->sessionFile == "/s/cc-2.jsonl");
    ui::ompSessionLinkRekey("missing", "cX");  // no-op, never invents an entry
    assert(ui::ompSessionLink("cX") == nullptr);

    // ensure() mints an empty (non-persisting) entry; erase drops it (hard delete).
    ui::ompSessionLinkEnsure("c12").runtimeLive = true;
    assert(ui::ompSessionLink("c12")->runtimeLive);
    ui::ompSessionLinkErase("c12");
    assert(ui::ompSessionLink("c12") == nullptr);
    ui::ompSessionLinksClear();
}

}  // namespace

int main() {
    test_method_names_are_contract_frozen();
    test_export_params_omit_empty_output_path();
    test_branch_close_params();
    test_action_error_prefers_reason_then_error();
    test_session_not_live_is_a_clean_refusal();
    test_rpc_absent_latch_conditions();
    test_export_parses_path_and_rejects_refusal();
    test_branch_requires_both_child_keys();
    test_close_is_ok_envelope_only();
    test_link_json_round_trip_and_empty_elision();
    test_persist_adds_only_the_omp_key();
    test_legacy_link_migrates_without_rewriting_history();
    test_registry_rekey_erase_and_ensure();
    std::printf("test_omp_session_actions: all tests passed\n");
    return 0;
}
