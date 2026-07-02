// ask_form smoke tests — the in-app-dialog transport's transport-neutral model
// (contracts/interaction.md §3/§4). Built + registered from ui/abstract/CMakeLists.txt
// (aicad_ui_abstract has no ImGui dependency, so this stays a fast, lock-free target).
#include "ask_form.h"

#include <nlohmann/json.hpp>

#include <cassert>
#include <map>
#include <string>

using aicad::ui::AskField;
using aicad::ui::AskFieldKind;
using aicad::ui::AskForm;
using aicad::ui::buildAnswersJson;
using aicad::ui::parseAskBatch;

int main() {
    // Sample §3 batch: one dimension question (typed suggested default) + one choice question,
    // straight from the contract's §11 worked example.
    const std::string batchJson = R"({
        "schema": "aicad.interaction/1",
        "batch_id": "b_1",
        "scope": "run",
        "run_id": "r_9",
        "blocking": true,
        "questions": [
            {"id": "bore.dia", "kind": "dimension", "family": "length",
             "prompt": "Bore diameter?", "suggested": {"value": 10, "unit": "mm"},
             "required": true, "why": "Sets the bearing."},
            {"id": "finish", "kind": "choice", "prompt": "Surface finish?", "required": false,
             "options": [{"value": "as_machined"}, {"value": "anodized"}],
             "suggested": "as_machined"}
        ]
    })";

    AskForm form = parseAskBatch(batchJson);
    assert(form.batchId == "b_1");
    assert(form.scope == "run");
    assert(form.blocking == true);
    assert(form.fields.size() == 2);

    const AskField& bore = form.fields[0];
    assert(bore.id == "bore.dia");
    assert(bore.kind == AskFieldKind::Dimension);
    assert(bore.family == "length");
    assert(bore.required == true);
    assert(bore.prompt == "Bore diameter?");
    {
        nlohmann::json sug = nlohmann::json::parse(bore.suggestedJson);
        assert(sug["value"].get<double>() == 10.0);
        assert(sug["unit"].get<std::string>() == "mm");
    }

    const AskField& finish = form.fields[1];
    assert(finish.id == "finish");
    assert(finish.kind == AskFieldKind::Choice);
    assert(finish.required == false);
    assert(finish.options.size() == 2);
    assert(finish.options[0] == "as_machined");
    assert(finish.options[1] == "anodized");

    // Collected raw widget values (dimension raw is "<num> <unit>" per the widget contract).
    std::map<std::string, std::string> rawValues = {
        {"bore.dia", "0.5 in"},
        {"finish", "anodized"},
    };
    std::string answersJson = buildAnswersJson(form, rawValues);
    nlohmann::json env = nlohmann::json::parse(answersJson);

    assert(env["schema"] == "aicad.interaction.answers/1");
    assert(env["batch_id"] == "b_1");

    const nlohmann::json& answers = env["answers"];
    assert(answers["bore.dia"]["value"]["value"].get<double>() == 0.5);
    assert(answers["bore.dia"]["value"]["unit"].get<std::string>() == "in");
    assert(answers["finish"]["value"].get<std::string>() == "anodized");

    // Unknown question kinds are tolerated (skipped), per contract §10.
    const std::string batchWithUnknown = R"({
        "schema": "aicad.interaction/1", "batch_id": "b_2", "scope": "run", "blocking": true,
        "questions": [
            {"id": "x", "kind": "future_kind", "prompt": "?", "suggested": null},
            {"id": "y", "kind": "text", "prompt": "Name?", "suggested": "widget"}
        ]
    })";
    AskForm form2 = parseAskBatch(batchWithUnknown);
    assert(form2.fields.size() == 1);
    assert(form2.fields[0].id == "y");
    assert(form2.fields[0].kind == AskFieldKind::Text);

    // Malformed JSON yields an empty form rather than crashing.
    AskForm bad = parseAskBatch("{not json");
    assert(bad.batchId.empty());
    assert(bad.fields.empty());

    return 0;
}
