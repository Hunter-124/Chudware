// test_ask_answers.cpp — ctest for the ask_user question card's GL-free half
// (src/ui/copilot/ask_answers.cpp). No GL, ImGui or AppState.
//
// Acceptance:
//   - suggested defaults prefill each kind (choice by value or label, unmatched -> Other text,
//     dimension value/unit with a family-default unit, approval bool, geometry picks)
//   - the RAW answer per kind matches the `answer` control action contract
//   - required questions without an answer (or a malformed dimension) block Submit
//   - agent.answer folds into the batch; a user resolution is never downgraded to cancelled
//   - the session-file round trip keeps questions + answers; an open batch restores Expired

#include "ui/copilot/ask_answers.h"

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

using nlohmann::json;

static json questionEvent() {
    return json::parse(R"({
      "run_id": "r_1", "session_id": "s_1", "node_id": "n_1", "batch_id": "b_abc",
      "prompt": "Which finish?",
      "questions": [
        {"id": "finish", "kind": "choice", "prompt": "Which finish?", "required": true,
         "suggested": "Anodized",
         "options": [{"value": "as_machined", "label": "As machined"},
                     {"value": "anodized", "label": "Anodized"}], "multi": false},
        {"id": "features", "kind": "choice", "prompt": "Which features?", "multi": true,
         "suggested": ["fillets", "knurl"],
         "options": [{"value": "fillets"}, {"value": "chamfers"}]},
        {"id": "bore", "kind": "dimension", "family": "length", "prompt": "Bore?",
         "required": true, "suggested": {"value": 10, "unit": "in"}},
        {"id": "ok", "kind": "approval", "prompt": "Proceed?", "proposal": "Cut the slot",
         "suggested": true}
      ]})");
}

static void testPrefillAndAnswers() {
    AskBatch b = askBatchFromEvent(questionEvent());
    CHECK(b.batchId == "b_abc");
    CHECK(b.runId == "r_1");
    CHECK(b.questions.size() == 4);

    // choice single: suggested matched the LABEL -> sends the option VALUE.
    const AskQuestion& fin = b.questions[0];
    CHECK(fin.choice == 1);
    CHECK(askAnswerValue(fin) == json("anodized"));

    // choice multi: one option matched, the unmatched suggestion becomes Other text.
    const AskQuestion& feat = b.questions[1];
    CHECK(feat.checked.size() == 2 && feat.checked[0] == 1 && feat.checked[1] == 0);
    CHECK(feat.otherOn);
    CHECK(askAnswerValue(feat) == json::parse(R"(["fillets", "knurl"])"));

    // dimension: typed {value, unit}; the suggested unit is kept when the family allows it.
    const AskQuestion& bore = b.questions[2];
    CHECK(bore.unit == "in");
    CHECK(askAnswerValue(bore) == json::parse(R"({"value": 10.0, "unit": "in"})"));

    // approval: bool.
    CHECK(askAnswerValue(b.questions[3]) == json(true));

    CHECK(askBatchReady(b));
    const json answers = buildAskAnswers(b);
    CHECK(answers.size() == 4);
    CHECK(answers["finish"] == "anodized");
    CHECK(answers["ok"] == true);
}

static void testOtherAndEdits() {
    AskBatch b = askBatchFromEvent(questionEvent());
    AskQuestion& fin = b.questions[0];
    // The user's own words for a single choice.
    fin.choice = kAskOther;
    askBufSet(fin.otherBuf, "  bead blasted ");
    CHECK(askAnswerValue(fin) == json("bead blasted"));
    // Other selected but empty: unanswered -> required blocks Submit.
    askBufSet(fin.otherBuf, "");
    CHECK(askAnswerValue(fin).is_null());
    std::string why;
    CHECK(!askBatchReady(b, &why));
    CHECK(why.find("Which finish?") != std::string::npos);
    fin.choice = 0;
    CHECK(askBatchReady(b));

    // Multi with nothing ticked and not required: an explicit empty list ("none of these").
    AskQuestion& feat = b.questions[1];
    feat.checked.assign(2, 0);
    feat.otherOn = false;
    CHECK(askAnswerValue(feat) == json::array());

    // Malformed dimension blocks Submit even though it has text.
    AskQuestion& bore = b.questions[2];
    askBufSet(bore.textBuf, "12mm");
    CHECK(!askBatchReady(b, &why));
    CHECK(why.find("number") != std::string::npos);
    askBufSet(bore.textBuf, "12.5");
    bore.unit = "mm";
    CHECK(askAnswerValue(bore) == json::parse(R"({"value": 12.5, "unit": "mm"})"));

    // Approval declined.
    b.questions[3].approval = 0;
    CHECK(askAnswerValue(b.questions[3]) == json(false));
}

static void testOtherKinds() {
    const json ev = json::parse(R"({
      "batch_id": "b_2", "run_id": "r_2",
      "questions": [
        {"id": "angle", "kind": "dimension", "family": "angle", "prompt": "Draft?",
         "suggested": {"value": 2, "unit": "furlong"}},
        {"id": "n", "kind": "dimension", "family": "count", "prompt": "How many?", "suggested": 4},
        {"id": "note", "kind": "text", "prompt": "Notes?", "multiline": true, "suggested": "none"},
        {"id": "ref", "kind": "file_request", "prompt": "Reference?", "media": "image",
         "multiple": true, "suggested": null, "required": true},
        {"id": "face", "kind": "geometry_select", "prompt": "Which face?", "targets": ["face"],
         "suggested": {"kind": "face", "id": "Fab12", "label": "top face"}},
        {"id": "mystery", "kind": "colour_wheel", "prompt": "Colour?", "suggested": "red"}
      ]})");
    AskBatch b = askBatchFromEvent(ev);
    CHECK(b.questions.size() == 6);
    // Family-mismatched suggested unit falls back to the family default.
    CHECK(b.questions[0].unit == "deg");
    CHECK(askAnswerValue(b.questions[0]) == json::parse(R"({"value": 2.0, "unit": "deg"})"));
    // count: integral value, unit "count".
    CHECK(askAnswerValue(b.questions[1]) == json::parse(R"({"value": 4, "unit": "count"})"));
    CHECK(askAnswerValue(b.questions[2]) == json("none"));
    // file_request: required and empty blocks Submit; paths split on ';'.
    CHECK(!askBatchReady(b));
    askBufSet(b.questions[3].textBuf, "/tmp/a.png; /tmp/b.jpg ;");
    CHECK(askAnswerValue(b.questions[3]) ==
          json::parse(R"({"paths": ["/tmp/a.png", "/tmp/b.jpg"]})"));
    CHECK(askBatchReady(b));
    // geometry_select single -> one selection object.
    CHECK(askAnswerValue(b.questions[4]) ==
          json::parse(R"({"kind": "face", "id": "Fab12", "label": "top face"})"));
    // Unknown kind answers as text.
    CHECK(askAnswerValue(b.questions[5]) == json("red"));
    // Summary text.
    CHECK(askAnswerText(b.questions[1], askAnswerValue(b.questions[1])) == "4");
    CHECK(askAnswerText(b.questions[3], askAnswerValue(b.questions[3])) == "a.png, b.jpg");
    CHECK(askAnswerText(b.questions[4], askAnswerValue(b.questions[4])) == "top face");
}

static void testAnswerEventsAndPersistence() {
    AskBatch b = askBatchFromEvent(questionEvent());
    b.status = AskStatus::Submitting;
    applyAskAnswerEvent(b, json::parse(R"({"batch_id": "b_abc", "source": "user",
                                           "answers": {"finish": "anodized"}})"));
    CHECK(b.status == AskStatus::Answered);
    CHECK(b.answers["finish"] == "anodized");
    // A late turn-end cancel never downgrades the user's resolution.
    applyAskAnswerEvent(b, json::parse(R"({"batch_id": "b_abc", "source": "cancelled"})"));
    CHECK(b.status == AskStatus::Answered);

    AskBatch r = askBatchFromEvent(questionEvent());
    applyAskAnswerEvent(r, json::parse(R"({"source": "reply", "reply": "use 8 mm"})"));
    CHECK(r.status == AskStatus::Reply);
    CHECK(r.reply == "use 8 mm");

    // Round trip: resolved state survives; question objects come back verbatim.
    const AskBatch back = askBatchFromJson(askBatchToJson(b));
    CHECK(back.status == AskStatus::Answered);
    CHECK(back.batchId == "b_abc");
    CHECK(back.questions.size() == 4);
    CHECK(back.questions[2].family == "length");
    CHECK(back.answers["finish"] == "anodized");

    // A batch still open when saved restores Expired with a reason.
    AskBatch open = askBatchFromEvent(questionEvent());
    const AskBatch reopened = askBatchFromJson(askBatchToJson(open));
    CHECK(reopened.status == AskStatus::Expired);
    CHECK(!reopened.error.empty());
}

int main() {
    testPrefillAndAnswers();
    testOtherAndEdits();
    testOtherKinds();
    testAnswerEventsAndPersistence();
    if (g_failures) {
        std::printf("test_ask_answers: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_ask_answers: ok\n");
    return 0;
}
