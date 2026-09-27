// See rules_authoring.h — the compact guided-chat popup that creates a Skill, a Context pack
// or a Rule through the EXISTING worker RPCs (skills_save / packs_save / triggers_save).
//
// Deliberately model-free. The panel asks scripted questions, echoes the answers as chat
// bubbles, then shows the EXACT text it is about to write before enabling Create. Nothing is
// invented on the user's behalf, and there is no second persistence path: every artifact lands
// through the same RPC the Settings tabs already use.
#include "rules_authoring.h"

#include <algorithm>
#include <cstring>
#include <string>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect

#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui_theme.h"    // palette / uiScale / adaptivePanel / glassPanel / glassRowBackground
#include "ui/metrics.h"  // cpMenu* / cpChat* rhythm tokens
#include "ui/copilot/copilot_internal.h"  // userFacingRpcError / kAvailableAfterRun / aiTurnActive

namespace ui {
namespace {

// glassPanel / glassRowBackground / coloredRowWash take a LOGICAL dp radius (they scale it
// internally); cpMenuRowRound() already returns PIXELS, so hand them the dp value back.
inline float rowRoundDp() { return ui::metrics::cpMenuRowRound() / ui::uiScale; }

// The palette exposes roles as float[4]; ImGui's style stack wants an ImVec4.
inline ImVec4 v4(const float c[4]) { return ImVec4(c[0], c[1], c[2], c[3]); }

enum class Kind { Skill = 0, Pack = 1, Rule = 2 };

constexpr const char* kPopupId = "##rules_authoring";

// ---- state ------------------------------------------------------------------------------------

struct Bubble {
    bool user = false;
    std::string text;
};

struct AuthoringUi {
    bool opened = false;          // our OpenPopup for the current *open has been issued
    Kind kind = Kind::Rule;
    int  step = 0;                // index into the current kind's question script
    std::vector<Bubble> log;
    char input[8192] = {};
    bool focusInput = true;

    // Collected answers.
    std::string name;     // skill name / pack name / rule id
    std::string desc;     // skill description / rule purpose
    std::string trigger;  // rule match keywords, one per line
    std::string body;     // SKILL.md prose / pack body / injected context

    // Preview + result.
    bool        preview = false;
    bool        disabledRule = false;   // rule taken through the "no trigger" exit
    std::string previewPath;            // where it will land on disk
    std::vector<char> previewText{std::vector<char>(1, 0)};
    bool        saving = false;
    std::string error;
    bool        errorCalm = false;      // `error` is a transient "after this run" note, not a fault
    std::string donePath;               // non-empty once the artifact is on disk
};

AuthoringUi& AU() {
    static AuthoringUi s;
    return s;
}

// ---- question scripts -------------------------------------------------------------------------

struct Step {
    const char* ask;
    const char* hint;
};

const Step kSkillSteps[] = {
    {"What should this skill be called?",
     "lowercase, digits, dashes \xc2\xb7 becomes the folder name"},
    {"One line: when should the agent reach for it?",
     "this is the description the agent matches against"},
    {"Now the skill itself \xe2\x80\x94 what does the agent need to know?",
     "markdown; written as the body of SKILL.md"},
};
const Step kPackSteps[] = {
    {"Name for this context pack?",
     "lowercase, digits, dashes \xc2\xb7 becomes <name>.md"},
    {"What text should be injected?",
     "markdown, injected verbatim when a rule references this pack"},
};
const Step kRuleSteps[] = {
    {"Give the rule an id.",
     "lowercase, digits, dashes \xc2\xb7 shown in the Rules list"},
    {"What is this rule for?",
     "one line; the purpose shown beside it"},
    {"What should trigger it?",
     "one keyword per line \xc2\xb7 matched as case-insensitive regexes"},
    {"And what should be injected when it fires?",
     "markdown; saved as this rule's context pack"},
};

const Step* stepsFor(Kind k, int* count) {
    switch (k) {
        case Kind::Skill: *count = 3; return kSkillSteps;
        case Kind::Pack:  *count = 2; return kPackSteps;
        case Kind::Rule:  *count = 4; return kRuleSteps;
    }
    *count = 0;
    return nullptr;
}

const char* kindLabel(Kind k) {
    switch (k) {
        case Kind::Skill: return "Skill";
        case Kind::Pack:  return "Context pack";
        case Kind::Rule:  return "Rule";
    }
    return "";
}

const char* kindOpener(Kind k) {
    switch (k) {
        case Kind::Skill:
            return "A skill is a named playbook the agent can pull in on demand. "
                   "I'll ask three things, then show you the SKILL.md before writing it.";
        case Kind::Pack:
            return "A context pack is a block of markdown a rule can inject. "
                   "Two questions, then you approve the exact text.";
        case Kind::Rule:
            return "A rule watches the conversation and injects context when it matches. "
                   "Four questions \xe2\x80\x94 the trigger is required, because a rule "
                   "without one can never fire.";
    }
    return "";
}

// ---- text helpers -----------------------------------------------------------------------------

std::string trim(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && (unsigned char)s[a] <= ' ') ++a;
    while (b > a && (unsigned char)s[b - 1] <= ' ') --b;
    return s.substr(a, b - a);
}

// Skill names: services/agent/skills _NAME_RE = ^[a-z0-9][a-z0-9_-]*$ (a leading digit is
// legal). We additionally cap at 64 chars so a skill name can also be reused verbatim as a
// pack/rule id if the user wants.
bool validSkillName(const std::string& s) {
    if (s.empty() || s.size() > 64) return false;
    const char c0 = s[0];
    if (!((c0 >= 'a' && c0 <= 'z') || (c0 >= '0' && c0 <= '9'))) return false;
    for (char c : s) {
        const bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_';
        if (!ok) return false;
    }
    return true;
}

// Rule ids AND pack names go through advisor/triggers _ID_RE / _PACK_RE
// (^[A-Za-z][A-Za-z0-9_-]{0,63}$), which require a LETTER first — a leading digit that a skill
// name would accept is rejected there, so both kinds use this stricter check.
bool validPackOrRuleId(const std::string& s) {
    return validSkillName(s) && s[0] >= 'a' && s[0] <= 'z';
}

std::vector<std::string> splitLines(const std::string& s) {
    std::vector<std::string> out;
    size_t i = 0;
    while (i <= s.size()) {
        size_t j = s.find('\n', i);
        if (j == std::string::npos) j = s.size();
        const std::string line = trim(s.substr(i, j - i));
        if (!line.empty()) out.push_back(line);
        if (j == s.size()) break;
        i = j + 1;
    }
    return out;
}

void setPreviewText(AuthoringUi& A, const std::string& text) {
    A.previewText.assign(text.begin(), text.end());
    A.previewText.push_back('\0');
}

// ---- artifact composition (exactly what gets written) -----------------------------------------

std::string skillMd(const AuthoringUi& A) {
    return "---\nname: " + A.name + "\ndescription: " + A.desc + "\n---\n\n" + A.body + "\n";
}

nlohmann::json ruleTrigger(const AuthoringUi& A) {
    nlohmann::json t;
    t["id"] = A.name;
    t["enabled"] = !A.disabledRule;
    t["purpose"] = A.desc;
    nlohmann::json kws = nlohmann::json::array();
    for (const std::string& k : splitLines(A.trigger)) kws.push_back(k);
    t["match"] = nlohmann::json{{"keywords", kws},
                                {"tools", nlohmann::json::array()},
                                {"events", nlohmann::json::array()}};
    t["inject"] = nlohmann::json{{"context_pack", A.name},
                                 {"prerun_calcs", nlohmann::json::array()},
                                 {"exemplar", ""}};
    t["budget_tokens"] = 250;
    t["cooldown_turns"] = 8;
    return t;
}

void buildPreview(AuthoringUi& A) {
    A.preview = true;
    A.error.clear();
    switch (A.kind) {
        case Kind::Skill:
            A.previewPath = "skills/" + A.name + "/SKILL.md";
            setPreviewText(A, skillMd(A));
            break;
        case Kind::Pack:
            A.previewPath = "context_injection/packs/" + A.name + ".md";
            setPreviewText(A, A.body + "\n");
            break;
        case Kind::Rule:
            A.previewPath = "context_injection/triggers.json  +  packs/" + A.name + ".md";
            setPreviewText(A, ruleTrigger(A).dump(2) +
                                  "\n\n--- packs/" + A.name + ".md ---\n\n" + A.body + "\n");
            break;
    }
}

// ---- conversation advance ---------------------------------------------------------------------

void resetAnswers(AuthoringUi& A) {
    A.step = 0;
    A.log.clear();
    A.input[0] = '\0';
    A.name.clear();
    A.desc.clear();
    A.trigger.clear();
    A.body.clear();
    A.preview = false;
    A.disabledRule = false;
    A.previewPath.clear();
    setPreviewText(A, "");
    A.saving = false;
    A.error.clear();
    A.donePath.clear();
    A.focusInput = true;
    A.log.push_back(Bubble{false, kindOpener(A.kind)});
    int n = 0;
    if (const Step* s = stepsFor(A.kind, &n)) A.log.push_back(Bubble{false, s[0].ask});
}

// Record the user's answer for the current step. Returns "" on success, or the complaint the
// panel should say back so the user can fix it without losing anything already collected.
std::string accept(AuthoringUi& A, const std::string& answer) {
    const bool rule = (A.kind == Kind::Rule);
    if (A.step == 0) {
        const std::string n = trim(answer);
        const bool ok = (A.kind == Kind::Skill) ? validSkillName(n) : validPackOrRuleId(n);
        if (!ok) {
            return rule
                ? "That id won't work. Start with a letter, then letters, digits, dashes or "
                  "underscores \xe2\x80\x94 up to 64 characters."
                : "That name won't work. Start with a letter, then lowercase letters, digits, "
                  "dashes or underscores \xe2\x80\x94 up to 64 characters.";
        }
        A.name = n;
        return "";
    }
    if (A.kind == Kind::Pack) {
        // step 1 = body
        if (trim(answer).empty()) return "I need the text to inject \xe2\x80\x94 it can't be empty.";
        A.body = trim(answer);
        return "";
    }
    if (A.step == 1) {
        if (trim(answer).empty())
            return rule ? "Give me one line on what this rule is for."
                        : "Give me one line \xe2\x80\x94 it's how the agent decides to load it.";
        A.desc = trim(answer);
        return "";
    }
    if (A.kind == Kind::Skill) {
        // step 2 = body
        if (trim(answer).empty()) return "The skill body can't be empty.";
        A.body = trim(answer);
        return "";
    }
    // Rule, step 2 = trigger, step 3 = injected context.
    if (A.step == 2) {
        if (splitLines(answer).empty())
            return "A rule with no trigger can never fire. Give me at least one keyword, or use "
                   "\"No trigger\" below to save it disabled.";
        A.trigger = trim(answer);
        return "";
    }
    if (trim(answer).empty()) return "The injected context can't be empty.";
    A.body = trim(answer);
    return "";
}

void send(AuthoringUi& A) {
    const std::string answer(A.input);
    if (trim(answer).empty()) return;
    A.log.push_back(Bubble{true, trim(answer)});
    const std::string complaint = accept(A, answer);
    A.input[0] = '\0';
    A.focusInput = true;
    if (!complaint.empty()) {
        A.log.push_back(Bubble{false, complaint});
        return;
    }
    int n = 0;
    const Step* steps = stepsFor(A.kind, &n);
    ++A.step;
    if (A.step >= n) {
        A.log.push_back(Bubble{false, "Here's exactly what I'll write. Nothing is saved until you "
                                      "press Create."});
        buildPreview(A);
        return;
    }
    A.log.push_back(Bubble{false, steps[A.step].ask});
}

// The explicit "no trigger" exit: never produce a silently-dead rule.
void skipTrigger(AuthoringUi& A) {
    A.trigger.clear();
    A.disabledRule = true;
    A.log.push_back(Bubble{true, "(no trigger)"});
    A.log.push_back(Bubble{false, "Saving it disabled, then \xe2\x80\x94 with no trigger it could "
                                  "never fire. Add keywords in the Rules list to switch it on."});
    A.step = 3;
    A.log.push_back(Bubble{false, kRuleSteps[3].ask});
    A.input[0] = '\0';
    A.focusInput = true;
}

// ---- persistence (existing RPCs only) ---------------------------------------------------------

bool rpcAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        return e.find("unknown") != std::string::npos || e.find("not found") != std::string::npos ||
               e.find("not_found") != std::string::npos || e.find("method") != std::string::npos;
    }
    if (r.result.is_object()) {
        const std::string c = r.result.value("code", std::string());
        if (c == "unknown_method" || c == "method_not_found" || c == "not_implemented") return true;
        if (!r.result.value("ok", true)) {
            const std::string reason = r.result.value("reason", std::string());
            if (reason == "unknown_method" || reason == "method_not_found") return true;
        }
    }
    return false;
}

void create(AppState& st, AuthoringUi& A) {
    if (A.saving) return;
    A.saving = true;
    A.error.clear();
    A.errorCalm = false;

    std::string method;
    nlohmann::json p;
    switch (A.kind) {
        case Kind::Skill:
            method = "skills_save";
            p = nlohmann::json{{"name", A.name}, {"body", skillMd(A)}};
            break;
        case Kind::Pack:
            method = "packs_save";
            p = nlohmann::json{{"name", A.name}, {"body", A.body + "\n"}};
            break;
        case Kind::Rule:
            // triggers_save writes the trigger AND its pack body in one call (worker_rpc
            // _triggers_save honours `pack_body` + `pack_name`), so a rule never lands
            // pointing at a pack that doesn't exist.
            method = "triggers_save";
            p = nlohmann::json{{"trigger", ruleTrigger(A)},
                               {"pack_name", A.name},
                               {"pack_body", A.body + "\n"}};
            break;
    }

    const std::string path = A.previewPath;
    st.rpc.call(method, p, [path](const core::RpcResult& r) {
        AuthoringUi& B = AU();
        B.saving = false;
        if (core::workerLaneBusy(r)) {
            B.error = kAvailableAfterRun;
            B.errorCalm = true;
            return;
        }
        B.errorCalm = false;
        if (rpcAbsent(r)) {
            B.error = "Saving isn't available in this version.";
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) {
            B.error = userFacingRpcError(r, "Couldn't save it. Try again.");
            return;
        }
        B.donePath = path;
        B.log.push_back(Bubble{false, "Written. It's live \xe2\x80\x94 the list behind this "
                                      "popup picks it up on its next refresh."});
    });
}

// ---- drawing ----------------------------------------------------------------------------------

// One chat bubble. Text goes through ImGui's wrapper (never dl->AddText) so it can neither
// escape the bubble nor run past the panel edge. `availW` comes from the caller because the
// scroll child reserves a right-hand lane for the pill scrollbar that GetContentRegionAvail()
// knows nothing about.
void drawBubble(const AdaptiveSurface& surf, const Bubble& b, float availW) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float padX = ui::metrics::cpChatCardPadX();
    const float padY = ui::metrics::cpChatCardPadY() * 2.0f;
    const float maxW = std::max(availW * 0.90f, padX * 4.0f);
    const float innerMax = maxW - padX * 2.0f;

    const ImVec2 ts = ImGui::CalcTextSize(b.text.c_str(), b.text.c_str() + b.text.size(),
                                          false, innerMax);
    const float w = std::min(maxW, ts.x + padX * 2.0f);
    const float h = ts.y + padY * 2.0f;

    const ImVec2 startLocal  = ImGui::GetCursorPos();
    const ImVec2 startScreen = ImGui::GetCursorScreenPos();
    const float offX = b.user ? (availW - w) : 0.0f;

    const ImVec2 mn(startScreen.x + offX, startScreen.y);
    const ImVec2 mx(mn.x + w, mn.y + h);
    if (b.user)
        coloredRowWash(dl, mn, mx, rowRoundDp(), ui::palette.accent, 0.30f);
    else
        glassRowBackground(dl, mn, mx, rowRoundDp(), false, false);

    ImGui::SetCursorPos(ImVec2(startLocal.x + offX + padX, startLocal.y + padY));
    ImGui::PushTextWrapPos(ImGui::GetCursorPos().x + (w - padX * 2.0f));
    ImGui::PushStyleColor(ImGuiCol_Text, b.user ? surf.textPrimary : surf.textSecondary);
    ImGui::TextUnformatted(b.text.c_str());
    ImGui::PopStyleColor();
    ImGui::PopTextWrapPos();

    ImGui::SetCursorPos(ImVec2(startLocal.x, startLocal.y + h + ui::metrics::cpChatTurnGap()));
}

// Segmented kind selector. Switching kind restarts the (short) conversation, which is the only
// honest thing to do: the questions differ.
void drawKindTabs(AuthoringUi& A) {
    const Kind kinds[3] = {Kind::Skill, Kind::Pack, Kind::Rule};
    const float gap = ui::metrics::cpMenuRowGap();
    const float w = (ImGui::GetContentRegionAvail().x - gap * 2.0f) / 3.0f;
    for (int i = 0; i < 3; ++i) {
        if (i) ImGui::SameLine(0.0f, gap);
        const bool sel = (A.kind == kinds[i]);
        if (sel) {
            ImGui::PushStyleColor(ImGuiCol_Button, v4(ui::palette.accentMid));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered, v4(ui::palette.accentMid));
        }
        if (ImGui::Button(kindLabel(kinds[i]), ImVec2(w, 0.0f)) && !sel) {
            A.kind = kinds[i];
            resetAnswers(A);
        }
        if (sel) ImGui::PopStyleColor(2);
    }
}

void drawComposer(AppState& st, AuthoringUi& A) {
    int n = 0;
    const Step* steps = stepsFor(A.kind, &n);
    const int idx = std::min(A.step, n - 1);

    ImGui::PushStyleColor(ImGuiCol_Text, v4(ui::palette.textDim));
    ImGui::TextWrapped("%s", steps[idx].hint);
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0.0f, ui::metrics::cpMenuLabelGap()));

    const float btnW = ui::metrics::px(72.0f);
    const float gap = ui::metrics::cpMenuRowGap();
    const float boxH = ImGui::GetTextLineHeight() * 3.0f + ImGui::GetStyle().FramePadding.y * 2.0f;
    if (A.focusInput) {
        ImGui::SetKeyboardFocusHere();
        A.focusInput = false;
    }
    ImGui::InputTextMultiline("##rules_auth_in", A.input, sizeof(A.input),
                              ImVec2(ImGui::GetContentRegionAvail().x - btnW - gap, boxH));
    // Ctrl+Enter sends (a bare Enter has to stay a newline: two of the steps are multi-line).
    const bool ctrlEnter = ImGui::IsItemFocused() && ImGui::GetIO().KeyCtrl &&
                           ImGui::IsKeyPressed(ImGuiKey_Enter, false);
    ImGui::SameLine(0.0f, gap);
    const bool canSend = !trim(std::string(A.input)).empty();
    ImGui::BeginDisabled(!canSend);
    const bool clicked = ImGui::Button("Send", ImVec2(btnW, boxH));
    ImGui::EndDisabled();
    if ((clicked || (ctrlEnter && canSend))) send(A);

    // The trigger step gets the explicit disabled-save exit.
    if (A.kind == Kind::Rule && A.step == 2) {
        ImGui::PushStyleColor(ImGuiCol_Text, v4(ui::palette.textDim));
        if (ImGui::SmallButton("No trigger \xc2\xb7 save disabled")) skipTrigger(A);
        ImGui::PopStyleColor();
    }
    (void)st;
}

void drawPreviewPane(AppState& st, AuthoringUi& A) {
    ImGui::PushStyleColor(ImGuiCol_Text, v4(ui::palette.textDim));
    ImGui::TextWrapped("Will write  %s", A.previewPath.c_str());
    ImGui::PopStyleColor();
    if (A.disabledRule) {
        ImGui::PushStyleColor(ImGuiCol_Text, v4(ui::palette.statusWarn));
        ImGui::TextWrapped("enabled: false \xe2\x80\x94 no trigger, so it cannot fire yet.");
        ImGui::PopStyleColor();
    }
    ImGui::Dummy(ImVec2(0.0f, ui::metrics::cpMenuLabelGap()));

    const float boxH = ImGui::GetTextLineHeightWithSpacing() * 7.0f;
    ImGui::InputTextMultiline("##rules_auth_preview", A.previewText.data(), A.previewText.size(),
                              ImVec2(-1.0f, boxH), ImGuiInputTextFlags_ReadOnly);

    if (!A.error.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
                              v4(A.errorCalm ? ui::palette.textDim : ui::palette.statusErr));
        ImGui::TextWrapped("%s", A.error.c_str());
        ImGui::PopStyleColor();
    }

    const float gap = ui::metrics::cpMenuRowGap();
    if (A.donePath.empty()) {
        // Saving shares the service a running agent run holds: say when it opens instead of
        // firing a call that is refused.
        const bool runBusy = aiTurnActive(st) || st.anyRunActive();
        if (ui::kit::primaryButton("Create", !A.saving && !runBusy,
                                   runBusy ? kAvailableAfterRun : nullptr,
                                   ImVec2(ui::metrics::px(96.0f), 0.0f)))
            create(st, A);
        ImGui::SameLine(0.0f, gap);
        if (ImGui::Button("Back", ImVec2(ui::metrics::px(72.0f), 0.0f))) {
            A.preview = false;
            A.error.clear();
            int n = 0;
            stepsFor(A.kind, &n);
            A.step = n - 1;
            A.focusInput = true;
        }
        if (A.saving) {
            ImGui::SameLine(0.0f, gap);
            ImGui::PushStyleColor(ImGuiCol_Text, v4(ui::palette.textDim));
            ImGui::TextUnformatted("saving\xe2\x80\xa6");
            ImGui::PopStyleColor();
        }
    } else {
        if (ImGui::Button("Create another", ImVec2(ui::metrics::px(136.0f), 0.0f)))
            resetAnswers(A);
    }
}

}  // namespace

void drawRulesAuthoringPopup(AppState& st, bool* open) {
    if (!open) return;
    AuthoringUi& A = AU();

    if (*open && !A.opened) {
        resetAnswers(A);
        ImGui::OpenPopup(kPopupId);
        A.opened = true;
    }
    if (!A.opened) return;

    const float S = ui::uiScale;
    ImGui::SetNextWindowSize(ImVec2(460.0f * S, 470.0f * S), ImGuiCond_Appearing);
    if (!ImGui::BeginPopupModal(kPopupId, nullptr,
                                ImGuiWindowFlags_NoSavedSettings |
                                    ImGuiWindowFlags_NoTitleBar)) {
        // Dismissed by Esc / click-away.
        A.opened = false;
        *open = false;
        return;
    }

    const ImVec2 wpos = ImGui::GetWindowPos();
    const ImRect wrect(wpos, ImVec2(wpos.x + ImGui::GetWindowSize().x,
                                    wpos.y + ImGui::GetWindowSize().y));
    const AdaptiveSurface& surf =
        adaptivePanel(panelId("rules_authoring_glass"), wrect, GlassGroup::Global);

    // ---- header ----
    ImGui::TextUnformatted("New");
    ImGui::SameLine(0.0f, ui::metrics::cpMenuRowGap());
    ImGui::PushStyleColor(ImGuiCol_Text, v4(ui::palette.textDim));
    ImGui::TextUnformatted(kindLabel(A.kind));
    ImGui::PopStyleColor();
    {
        const float closeW = ui::metrics::px(24.0f);
        ImGui::SameLine(ImGui::GetContentRegionMax().x - closeW);
        if (ImGui::SmallButton("\xc3\x97")) {
            A.opened = false;
            *open = false;
            ImGui::CloseCurrentPopup();
            ImGui::EndPopup();
            return;
        }
    }
    ImGui::Dummy(ImVec2(0.0f, ui::metrics::cpMenuLabelGap()));
    drawKindTabs(A);
    ImGui::Dummy(ImVec2(0.0f, ui::metrics::cpMenuHeadGap()));

    // ---- transcript ----
    // Reserve the bottom band for the composer / preview so the scrolling log takes the rest.
    const float bandH = A.preview ? ImGui::GetTextLineHeightWithSpacing() * 12.0f
                                  : ImGui::GetTextLineHeight() * 5.5f +
                                        ImGui::GetFrameHeightWithSpacing();
    const float logH = std::max(ImGui::GetContentRegionAvail().y - bandH,
                                ImGui::GetTextLineHeightWithSpacing() * 3.0f);

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuGutter(), ui::metrics::cpMenuRowPadY()));
    ImGui::BeginChild("##rules_auth_log", ImVec2(0.0f, logH),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ImVec2 c0 = ImGui::GetWindowPos();
        const ImVec2 c1(c0.x + ImGui::GetWindowSize().x, c0.y + ImGui::GetWindowSize().y);
        glassPanel(dl, ImRect(c0, c1), GlassLevel::Thin, rowRoundDp(), surf);
        // The pill scrollbar is drawn INSIDE this child's clip rect, so hand the bubbles a
        // content width that already excludes its lane — GetContentRegionAvail() doesn't know
        // about it and a right-aligned user bubble would slide under the bar.
        const float bubbleW = std::max(ImGui::GetContentRegionAvail().x -
                                           ui::metrics::px(ui::kChatScrollbarLane),
                                       ui::metrics::px(80.0f));
        for (const Bubble& b : A.log) drawBubble(surf, b, bubbleW);
        // Follow the conversation: a new bubble always scrolls into view. ScrollMaxY has not
        // grown yet on the frame the bubble is appended, so this is driven off the log length
        // rather than off the scroll position.
        static size_t s_lastLogLen = 0;
        if (A.log.size() != s_lastLogLen) {
            s_lastLogLen = A.log.size();
            ImGui::SetScrollHereY(1.0f);
        }
    }
    drawWindowPillScrollbarV(surf, ui::kChatScrollbar);
    ImGui::EndChild();
    ImGui::PopStyleVar();

    ImGui::Dummy(ImVec2(0.0f, ui::metrics::cpMenuHeadGap()));

    // ---- composer or preview ----
    if (A.preview)
        drawPreviewPane(st, A);
    else
        drawComposer(st, A);

    ImGui::EndPopup();
}

}  // namespace ui
