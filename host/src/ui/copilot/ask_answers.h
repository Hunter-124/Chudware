// ask_answers.h — the online agent's interactive `ask_user` question batch: the data model the
// chat's question card edits, plus every piece of logic that needs no ImGui (event ingest,
// suggested-default prefill, the RAW answer JSON sent over the `answer` control action, the
// read-only summary text, and the session-file form). GL-free: host/tests/test_ask_answers.cpp.
//
// Wire contract: contracts/interaction.md (question kinds, typed answer values) and the
// agent.question / agent.answer feed events. A batch holds at most four questions; unknown kinds
// and fields are tolerated (an unknown kind is edited and answered as free text).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

// pending    — waiting for the user (the card is interactive)
// submitting — the `answer` control call is in flight
// answered   — resolved by the card's Submit (agent.answer source "user")
// reply      — resolved by a message typed in the composer (source "reply")
// declined   — the user dismissed the card (source "declined")
// cancelled  — the run stopped or the turn ended before an answer (source "cancelled")
// expired    — the worker no longer knows the batch (its run ended / the app restarted)
enum class AskStatus { Pending, Submitting, Answered, Reply, Declined, Cancelled, Expired };

// One picked model entity for a geometry_select answer (contract §12 selection object).
struct AskPick {
    std::string kind, id, docId, label;
    nlohmann::json selector;   // engine selector when the host resolved the pick; null otherwise
};

struct AskOption {
    std::string value, label;
};

struct AskQuestion {
    nlohmann::json src = nlohmann::json::object();   // the question exactly as received (persisted)
    // ---- contract fields ----
    std::string id, kind = "text", prompt, why;
    nlohmann::json suggested;            // typed default; null when the model proposed none
    bool required = false;
    std::vector<AskOption> options;      // choice
    bool multi = false;                  // choice / geometry_select
    bool multiline = false;              // text
    std::string family;                  // dimension: length | angle | count
    std::string proposal;                // approval
    std::string media;                   // file_request: file | image
    std::vector<std::string> accept;     // file_request extensions
    bool multiple = false;               // file_request
    std::vector<std::string> targets;    // geometry_select
    std::string docId;                   // geometry_select
    // ---- edit state (GUI-only) ----
    int choice = -1;                     // choice single: option index, kAskOther, or -1 = none
    std::vector<unsigned char> checked;  // choice multi: one flag per option
    bool otherOn = false;                // choice multi: the "Other" row is ticked
    std::vector<char> otherBuf = std::vector<char>(512, 0);  // choice "Other" free text
    // text / unknown kinds: the answer; dimension: the value as typed; file_request: paths
    // separated by ';' or newlines.
    std::vector<char> textBuf = std::vector<char>(1024, 0);
    std::string unit;                    // dimension unit (one of askDimUnits(family))
    int approval = -1;                   // approval: 1 approve, 0 decline, -1 not chosen
    std::vector<AskPick> picks;          // geometry_select
};

struct AskBatch {
    std::string batchId, runId, sessionId, nodeId, agentId, prompt;
    std::vector<AskQuestion> questions;
    AskStatus status = AskStatus::Pending;
    std::string error;                   // last submit failure, or why the batch expired
    nlohmann::json answers = nlohmann::json::object();  // resolved answers {qid: RAW}
    std::string reply;                   // the composer reply that resolved it (status Reply)
};

// choice single: the "Other" row is selected.
constexpr int kAskOther = -2;

// The text in a NUL-terminated edit buffer.
std::string askBufStr(const std::vector<char>& buf);
// Replace a buffer's text, growing it so `s` always fits with headroom.
void askBufSet(std::vector<char>& buf, const std::string& s);

// Units offered for a dimension family (first entry = default): length mm,cm,m,in;
// angle deg,rad; count count. Unknown/empty families use the length list.
const std::vector<std::string>& askDimUnits(const std::string& family);

// Parse one contract question object and prefill its edit state from `suggested`.
AskQuestion askQuestionFromJson(const nlohmann::json& q);
// Parse an agent.question event payload (or a persisted batch's question list).
AskBatch askBatchFromEvent(const nlohmann::json& data);

// The question's RAW answer value per contract kind, or null when it has none yet.
nlohmann::json askAnswerValue(const AskQuestion& q);
// {qid: RAW} for every answered question (unanswered optional questions are omitted, so the
// worker applies their defaults).
nlohmann::json buildAskAnswers(const AskBatch& b);
// True when every required question has an answer and no typed value is malformed (a dimension
// that is not a number). `why` (optional) receives the first blocker as a sentence.
bool askBatchReady(const AskBatch& b, std::string* why = nullptr);

// Human-readable form of a RAW answer for the resolved card ("8 mm", "Approved", labels).
std::string askAnswerText(const AskQuestion& q, const nlohmann::json& raw);

// Status names (persisted) and agent.answer `source` mapping.
const char* askStatusName(AskStatus s);
AskStatus askStatusParse(const std::string& s);
bool askStatusOpen(AskStatus s);   // pending or submitting
// Fold an agent.answer payload into its batch. The worker is authoritative, except that a
// resolution the user already made (answered/reply/declined) is never downgraded to cancelled.
void applyAskAnswerEvent(AskBatch& b, const nlohmann::json& data);

// Session-file form. Questions round-trip as their original contract objects; edit state is not
// persisted. A batch restored still open is marked Expired: a restart drops every live run.
nlohmann::json askBatchToJson(const AskBatch& b);
AskBatch askBatchFromJson(const nlohmann::json& j);

// Selection object for a geometry_select answer: {kind, id, doc_id?, label?, selector?}.
nlohmann::json askPickToJson(const AskPick& p);
