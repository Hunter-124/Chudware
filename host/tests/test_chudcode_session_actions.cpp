// test_chudcode_session_actions.cpp — the Chudcode session-recovery seam behind the copilot
// Sessions panel (Manage card) and the conversation store's "chudcode" link. Links the REAL
// chudcode_session_actions.cpp (GL-free), so these are the shipped rules, not copies.
//
// The rules under test:
//   * the four contract-frozen method names and their exact request params (session_id,
//     optional output_path, entry_id, repair flag);
//   * parsers accept the worker's snake_case spellings (and tolerate camelCase passthrough)
//     but reject refusals, cancelled/empty branches and summary-less repairs instead of
//     coercing them into empty successes;
//   * "session_not_live" is a CLEAN refusal (resume hint), distinct from method absence
//     (surface latched off) and from transport errors;
//   * the per-conversation link round-trips id/file/health ONLY — persist adds exactly the
//     "chudcode" key and never touches the transcript ("msgs"), so no transcript is ever
//     duplicated or lost; runtimeLive is process-local and never serialized, which is what
//     makes every session INACTIVE after an app restart until a turn resumes it;
//   * re-keying a duplicate session id carries the link with the transcript it describes.
#include <cassert>
#include <cstdio>
#include <string>

#include <nlohmann/json.hpp>

#include "ui/chudcode_session_actions.h"

using nlohmann::json;

namespace {

// ---- request params ---------------------------------------------------------------------------

void test_method_names_are_contract_frozen() {
    assert(std::string(ui::kChudcodeExportSessionMethod) == "chudcode_export_session");
    assert(std::string(ui::kChudcodeBranchSessionMethod) == "chudcode_branch_session");
    assert(std::string(ui::kChudcodeRepairSessionMethod) == "chudcode_repair_session");
    assert(std::string(ui::kChudcodeCloseSessionMethod) == "chudcode_close_session");
}

void test_export_params_omit_empty_output_path() {
    // The worker guards unknown/odd params — a default export must send session_id ONLY.
    const json d = ui::chudcodeExportSessionParams("c7");
    assert(d.size() == 1 && d.at("session_id") == "c7");
    const json p = ui::chudcodeExportSessionParams("c7", "/tmp/out.html");
    assert(p.size() == 2 && p.at("output_path") == "/tmp/out.html");
}

void test_branch_repair_close_params() {
    const json b = ui::chudcodeBranchSessionParams("c7", "entry-42");
    assert(b.size() == 2 && b.at("session_id") == "c7" && b.at("entry_id") == "entry-42");
    const json dry = ui::chudcodeRepairSessionParams("c7", false);
    assert(dry.size() == 2 && dry.at("repair") == false);
    const json rep = ui::chudcodeRepairSessionParams("c7", true);
    assert(rep.at("repair") == true);
    const json cl = ui::chudcodeCloseSessionParams("c7");
    assert(cl.size() == 1 && cl.at("session_id") == "c7");
}

// ---- refusal / absence triage -----------------------------------------------------------------

void test_action_error_prefers_reason_then_error() {
    assert(ui::chudcodeActionError({{"ok", false}, {"reason", "nope"}}, "fb") == "nope");
    assert(ui::chudcodeActionError({{"ok", false}, {"error", "bad"}}, "fb") == "bad");
    assert(ui::chudcodeActionError(json("not an object"), "fb") == "fb");
}

void test_session_not_live_is_a_clean_refusal() {
    assert(ui::chudcodeSessionNotLive({{"ok", false}, {"reason", "session_not_live"}}));
    // Never a false positive on success or on an unrelated failure.
    assert(!ui::chudcodeSessionNotLive({{"ok", true}}));
    assert(!ui::chudcodeSessionNotLive({{"ok", false}, {"reason", "export failed"}}));
    assert(!ui::chudcodeSessionNotLive(json("x")));
}

void test_rpc_absent_latch_conditions() {
    // Transport-level unknown method (older worker).
    assert(ui::chudcodeSessionRpcAbsent(false, "unknown method chudcode_export_session", {}));
    // Envelope-level absence spellings.
    assert(ui::chudcodeSessionRpcAbsent(true, "", {{"ok", false}, {"reason", "unknown_method"}}));
    assert(ui::chudcodeSessionRpcAbsent(true, "", {{"code", "method_not_found"}}));
    // A live method refusing for a REAL reason must NOT latch the surface off.
    assert(!ui::chudcodeSessionRpcAbsent(true, "", {{"ok", false}, {"reason", "session_not_live"}}));
    assert(!ui::chudcodeSessionRpcAbsent(false, "timeout", {}));
}

// ---- export -----------------------------------------------------------------------------------

void test_export_parses_path_and_rejects_refusal() {
    ui::ChudcodeExportResult out;
    assert(ui::parseChudcodeExportResult(
        {{"ok", true}, {"path", "/proj/chat-exports/c7.html"}}, out));
    assert(out.path == "/proj/chat-exports/c7.html");
    // Refusal, missing path, and non-object are all failures — out untouched.
    out.path = "sentinel";
    assert(!ui::parseChudcodeExportResult({{"ok", false}, {"reason", "session_not_live"}}, out));
    assert(!ui::parseChudcodeExportResult({{"ok", true}}, out));
    assert(!ui::parseChudcodeExportResult(json("x"), out));
    assert(out.path == "sentinel");
}

// ---- branch -----------------------------------------------------------------------------------

void test_branch_requires_both_child_keys() {
    ui::ChudcodeBranchResult out;
    assert(ui::parseChudcodeBranchResult({{"ok", true},
                                          {"chudcode_session_id", "cc-9"},
                                          {"chudcode_session_file", "/s/cc-9.jsonl"}},
                                         out));
    assert(out.sessionId == "cc-9" && out.sessionFile == "/s/cc-9.jsonl");
    // A branch without a resume file (or id) would mint a dead fork — reject it.
    out.sessionId = "sentinel";
    assert(!ui::parseChudcodeBranchResult({{"ok", true}, {"chudcode_session_id", "cc-9"}}, out));
    assert(!ui::parseChudcodeBranchResult(
        {{"ok", true}, {"chudcode_session_file", "/s/x.jsonl"}}, out));
    assert(!ui::parseChudcodeBranchResult({{"ok", false}, {"reason", "cancelled"}}, out));
    assert(out.sessionId == "sentinel");
}

// ---- doctor (diagnose + repair) ---------------------------------------------------------------

void test_doctor_dry_run_summary() {
    const json r = {
        {"ok", true},
        {"mode", "dry-run"},
        {"status", "issues"},
        {"changed", false},
        {"finding_count", 2},
        {"repairable_finding_count", 1},
        {"remaining_finding_count", 2},
        {"findings",
         json::array({{{"code", "malformed_jsonl"},
                       {"message", "Malformed record skipped."},
                       {"repairable", true}},
                      {{"code", "orphaned_entry"},
                       {"message", "Entry parent is missing."},
                       {"repairable", false},
                       {"entry_id", "e-12"},
                       {"path", "content[0]"}}})},
        {"chudcode_session_id", "cc-3"},
        {"chudcode_session_file", "/s/cc-3.jsonl"},
    };
    ui::ChudcodeDoctorSummary s;
    assert(ui::parseChudcodeDoctorSummary(r, s));
    assert(s.mode == "dry-run" && s.status == "issues" && !s.changed);
    assert(s.findingCount == 2 && s.repairableFindingCount == 1 && s.remainingFindingCount == 2);
    assert(s.findings.size() == 2);
    assert(s.findings[0].repairable && s.findings[0].entryId.empty());
    assert(s.findings[1].entryId == "e-12" && s.findings[1].path == "content[0]");
    assert(s.sessionId == "cc-3" && s.sessionFile == "/s/cc-3.jsonl");
    assert(s.backupPath.empty());  // dry-run never writes a backup
}

void test_doctor_repair_carries_backup() {
    const json r = {{"ok", true},          {"mode", "repair"},
                    {"status", "repaired"}, {"changed", true},
                    {"backup_path", "/s/cc-3.jsonl.doctor-1.bak"}};
    ui::ChudcodeDoctorSummary s;
    assert(ui::parseChudcodeDoctorSummary(r, s));
    assert(s.status == "repaired" && s.changed);
    assert(s.backupPath == "/s/cc-3.jsonl.doctor-1.bak");
    assert(s.findingCount == 0);  // absent count derives from the (empty) findings list
}

void test_doctor_rejects_refusal_and_verdictless_payload() {
    ui::ChudcodeDoctorSummary s;
    s.status = "sentinel";
    assert(!ui::parseChudcodeDoctorSummary({{"ok", false}, {"reason", "session_not_live"}}, s));
    assert(!ui::parseChudcodeDoctorSummary({{"ok", true}}, s));  // no status = no summary
    assert(!ui::parseChudcodeDoctorSummary(json("x"), s));
    assert(s.status == "sentinel");
}

void test_close_is_ok_envelope_only() {
    assert(ui::parseChudcodeCloseResult({{"ok", true}}));                 // idempotent success
    assert(!ui::parseChudcodeCloseResult({{"ok", false}, {"reason", "x"}}));
    assert(!ui::parseChudcodeCloseResult(json("x")));
}

// ---- health vocabulary ------------------------------------------------------------------------

void test_health_classes_and_labels() {
    assert(ui::chudcodeHealthClass("healthy") == 0);
    assert(ui::chudcodeHealthClass("repaired") == 0);
    assert(ui::chudcodeHealthClass("issues") == 1);
    assert(ui::chudcodeHealthClass("repaired_with_issues") == 1);
    assert(ui::chudcodeHealthClass("") == 2);
    assert(ui::chudcodeHealthClass("garbage") == 2);
    assert(std::string(ui::chudcodeHealthLabel("")) == "not checked");
    assert(std::string(ui::chudcodeHealthLabel("issues")) == "issues found");
}

void test_apply_doctor_never_blanks_known_keys() {
    ui::ChudcodeSessionLink link;
    link.sessionId = "cc-1";
    link.sessionFile = "/s/cc-1.jsonl";
    link.health = "healthy";
    ui::ChudcodeDoctorSummary s;
    s.status = "issues";  // verdict updates; absent id/file must NOT blank the known ones
    ui::applyChudcodeDoctor(link, s);
    assert(link.health == "issues");
    assert(link.sessionId == "cc-1" && link.sessionFile == "/s/cc-1.jsonl");
    s.sessionFile = "/s/cc-1-moved.jsonl";  // reported keys ARE adopted
    ui::applyChudcodeDoctor(link, s);
    assert(link.sessionFile == "/s/cc-1-moved.jsonl");
}

// ---- link registry + conversation-JSON persistence --------------------------------------------

void test_link_json_round_trip_and_empty_elision() {
    ui::ChudcodeSessionLink link;
    link.sessionId = "cc-5";
    link.sessionFile = "/s/cc-5.jsonl";
    link.health = "healthy";
    link.runtimeLive = true;  // process-local — must NOT serialize
    const json j = ui::chudcodeSessionLinkToJson(link);
    assert(j.is_object());
    assert(j.at("session_id") == "cc-5" && j.at("session_file") == "/s/cc-5.jsonl" &&
           j.at("health") == "healthy");
    assert(!j.contains("runtimeLive") && !j.contains("runtime_live"));

    ui::ChudcodeSessionLink back;
    back.runtimeLive = true;  // pre-set to prove parse does not resurrect liveness…
    back.runtimeLive = false;
    assert(ui::chudcodeSessionLinkFromJson(j, back));
    assert(back.sessionId == "cc-5" && back.sessionFile == "/s/cc-5.jsonl" &&
           back.health == "healthy");
    assert(!back.runtimeLive);  // …a restored session is INACTIVE until a turn resumes it

    // Empty link -> null (persist skips the key entirely); junk -> rejected.
    assert(ui::chudcodeSessionLinkToJson(ui::ChudcodeSessionLink{}).is_null());
    ui::ChudcodeSessionLink untouched;
    untouched.sessionId = "sentinel";
    assert(!ui::chudcodeSessionLinkFromJson(json::object(), untouched));
    assert(!ui::chudcodeSessionLinkFromJson(json("x"), untouched));
    assert(untouched.sessionId == "sentinel");
}

void test_persist_adds_only_the_chudcode_key() {
    ui::chudcodeSessionLinksClear();
    ui::ChudcodeSessionLink link;
    link.sessionId = "cc-8";
    link.sessionFile = "/s/cc-8.jsonl";
    ui::chudcodeSessionLinkSet("c8", link);

    // A realistic conversation object: the transcript must come through byte-identical —
    // the link is metadata BESIDE the messages, never a second copy of them.
    json convObj = {{"id", "c8"},
                    {"title", "Bracket"},
                    {"msgs", json::array({{{"u", true}, {"t", "make a bracket"}},
                                          {{"u", false}, {"t", "done"}}})}};
    const json msgsBefore = convObj["msgs"];
    ui::chudcodeLinkPersist("c8", convObj);
    assert(convObj.contains("chudcode"));
    assert(convObj["chudcode"].at("session_file") == "/s/cc-8.jsonl");
    assert(convObj["msgs"] == msgsBefore);   // no transcript loss, no duplication
    assert(convObj.size() == 4);             // id + title + msgs + chudcode, nothing else

    // A conversation with no link gains NO key at all.
    json bare = {{"id", "c9"}, {"msgs", json::array()}};
    ui::chudcodeLinkPersist("c9", bare);
    assert(!bare.contains("chudcode"));

    // Restore round-trip into a clean registry (the load path).
    ui::chudcodeSessionLinksClear();
    assert(ui::chudcodeSessionLink("c8") == nullptr);
    ui::chudcodeLinkRestore("c8", convObj);
    const ui::ChudcodeSessionLink* got = ui::chudcodeSessionLink("c8");
    assert(got && got->sessionId == "cc-8" && got->sessionFile == "/s/cc-8.jsonl");
    assert(!got->runtimeLive);  // restart semantics: inactive until resumed
    ui::chudcodeSessionLinksClear();
}

void test_registry_rekey_erase_and_ensure() {
    ui::chudcodeSessionLinksClear();
    ui::ChudcodeSessionLink link;
    link.sessionFile = "/s/cc-2.jsonl";
    ui::chudcodeSessionLinkSet("c2", link);

    // persistChatHistory re-keys a duplicate id — the link follows the transcript.
    ui::chudcodeSessionLinkRekey("c2", "c12");
    assert(ui::chudcodeSessionLink("c2") == nullptr);
    const ui::ChudcodeSessionLink* moved = ui::chudcodeSessionLink("c12");
    assert(moved && moved->sessionFile == "/s/cc-2.jsonl");
    ui::chudcodeSessionLinkRekey("missing", "cX");  // no-op, never invents an entry
    assert(ui::chudcodeSessionLink("cX") == nullptr);

    // ensure() mints an empty (non-persisting) entry; erase drops it (hard delete).
    ui::chudcodeSessionLinkEnsure("c12").runtimeLive = true;
    assert(ui::chudcodeSessionLink("c12")->runtimeLive);
    ui::chudcodeSessionLinkErase("c12");
    assert(ui::chudcodeSessionLink("c12") == nullptr);
    ui::chudcodeSessionLinksClear();
}

}  // namespace

int main() {
    test_method_names_are_contract_frozen();
    test_export_params_omit_empty_output_path();
    test_branch_repair_close_params();
    test_action_error_prefers_reason_then_error();
    test_session_not_live_is_a_clean_refusal();
    test_rpc_absent_latch_conditions();
    test_export_parses_path_and_rejects_refusal();
    test_branch_requires_both_child_keys();
    test_doctor_dry_run_summary();
    test_doctor_repair_carries_backup();
    test_doctor_rejects_refusal_and_verdictless_payload();
    test_close_is_ok_envelope_only();
    test_health_classes_and_labels();
    test_apply_doctor_never_blanks_known_keys();
    test_link_json_round_trip_and_empty_elision();
    test_persist_adds_only_the_chudcode_key();
    test_registry_rekey_erase_and_ensure();
    std::printf("test_chudcode_session_actions: all tests passed\n");
    return 0;
}
