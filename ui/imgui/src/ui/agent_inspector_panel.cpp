// T-W17 Agent Inspector panel — see agent_inspector_panel.h.
//
// Architecture (pattern library §0/§1):
//   - View-model: aicad::ui::AgentInspectorModel (ui/abstract/agent_inspector_view.h)
//   - Scaffold: ui::drawPanelScaffold / ui::drawPanelEmptyState (T-U1 kit)
//   - Data: a REPLAYED run directory (events.jsonl + advisory/preflight/audit/
//     delegation/failure records). No worker RPC — this is the offline
//     troubleshooting window (§2.15).

#include "agent_inspector_panel.h"
#include "panel_scaffold.h"
#include "agent_inspector_view.h"   // ui/abstract/

#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/tokens.h"

#include "imgui.h"
#include <nlohmann/json.hpp>

#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

namespace ui {

namespace {

bool gOpen = false;
ui::PanelScaffoldState gScaffoldState;
aicad::ui::AgentInspectorModel gModel;
bool gForceDelegationTab = false;   // one-shot: open Delegation tab (shot use)

using aicad::ui::InspectorState;
using nlohmann::json;

// ---- run-dir parsing -------------------------------------------------------

std::string readFile(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) return {};
    std::stringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

json readJson(const std::string& path) {
    const std::string txt = readFile(path);
    if (txt.empty()) return json();
    try { return json::parse(txt); } catch (...) { return json(); }
}

// Map a preflight id to its verdict + first non-pass detail by reading the record.
void resolvePreflight(const std::string& runDir, const std::string& pfId,
                      std::string& verdict, std::string& detail) {
    verdict.clear(); detail.clear();
    if (pfId.empty()) return;
    const json rec = readJson(runDir + "/preflight/" + pfId + ".json");
    if (rec.is_null()) return;
    verdict = rec.value("verdict", std::string());
    if (rec.contains("checks") && rec["checks"].is_array()) {
        for (const auto& c : rec["checks"]) {
            if (c.value("result", std::string()) != "pass") {
                detail = c.value("detail", std::string());
                break;
            }
        }
    }
}

void parseDelegation(const std::string& runDir) {
    gModel.delegation.clear();
    const json d = readJson(runDir + "/delegation.json");
    if (d.is_null() || !d.contains("nodes")) return;
    // First pass: collect nodes.
    for (const auto& n : d["nodes"]) {
        aicad::ui::InspectorDelegationNode node;
        node.id     = n.value("id", std::string());
        node.parent = n.is_object() && n.contains("parent") && !n["parent"].is_null()
                          ? n["parent"].get<std::string>() : "";
        node.role   = n.value("role", std::string());
        node.brief  = n.value("brief", std::string());
        node.status = n.value("status", std::string());
        if (n.contains("model")) {
            const auto& m = n["model"];
            node.model = m.value("provider", std::string()) + "/" + m.value("id", std::string());
        }
        if (n.contains("approval")) {
            const auto& a = n["approval"];
            node.approval = a.value("policy", std::string());
            const std::string by = a.value("approved_by", std::string());
            if (!by.empty()) node.approval += " (" + by + ")";
        }
        if (n.contains("budget")) {
            const auto& b = n["budget"];
            node.tokens_granted = b.value("tokens_granted", 0);
            node.tokens_spent   = b.value("tokens_spent", 0);
            node.attempts       = b.value("attempts", 0);
        }
        gModel.delegation.push_back(std::move(node));
    }
    // Second pass: compute depth from parent chain (bounded to avoid cycles).
    for (auto& node : gModel.delegation) {
        int depth = 0;
        std::string cur = node.parent;
        for (int guard = 0; guard < 16 && !cur.empty(); ++guard) {
            ++depth;
            std::string next;
            for (const auto& p : gModel.delegation)
                if (p.id == cur) { next = p.parent; break; }
            cur = next;
        }
        node.depth = depth;
    }
}

void parseFailure(const std::string& runDir, const std::string& ref) {
    // ref is like "failures/fc_0001.json"
    const json fc = readJson(runDir + "/" + ref);
    if (fc.is_null()) return;
    aicad::ui::InspectorFailureCard card;
    card.id    = fc.value("id", std::string());
    card.klass = fc.value("class", std::string());
    card.stage = fc.value("stage", std::string());
    card.what  = fc.value("what", std::string());
    if (fc.contains("why")) card.why = fc["why"].value("detail", std::string());
    if (fc.contains("budget")) card.max_attempts = fc["budget"].value("max_attempts", 3);
    if (fc.contains("attempts") && fc["attempts"].is_array()) {
        for (const auto& a : fc["attempts"]) {
            aicad::ui::InspectorFailureAttempt at;
            at.n      = a.value("n", 0);
            at.result = a.value("result", std::string());
            if (a.contains("fix_applied") && a["fix_applied"].is_object()) {
                std::string s;
                for (auto it = a["fix_applied"].begin(); it != a["fix_applied"].end(); ++it) {
                    if (!s.empty()) s += ", ";
                    s += it.key() + "=" + (it->is_string() ? it->get<std::string>() : it->dump());
                }
                at.fix_applied = s;
            }
            if (a.contains("delta") && a["delta"].is_object())
                at.delta = a["delta"].value("status", a["delta"].dump());
            card.attempts.push_back(std::move(at));
        }
    }
    gModel.failures.push_back(std::move(card));
}

void parseAudit(const std::string& runDir, const std::string& ref, const std::string& stage) {
    const json a = readJson(runDir + "/" + ref);
    if (a.is_null()) return;
    aicad::ui::InspectorAudit au;
    au.id      = a.value("id", std::string());
    au.trigger = a.value("trigger", std::string());
    au.stage   = stage;
    if (a.contains("scope")) au.stage = a["scope"].value("stage", stage);
    if (a.contains("model")) {
        const auto& m = a["model"];
        au.model = m.value("provider", std::string()) + "/" + m.value("id", std::string());
    }
    if (a.contains("verdicts") && a["verdicts"].is_array()) {
        for (const auto& v : a["verdicts"]) {
            aicad::ui::InspectorAuditVerdict vd;
            vd.klass    = v.value("class", std::string());
            vd.finding  = v.value("finding", std::string());
            vd.severity = v.value("severity", std::string());
            if (v.contains("required_action")) {
                const auto& ra = v["required_action"];
                vd.action = ra.value("kind", std::string());
                const std::string det = ra.value("detail", std::string());
                if (!det.empty()) vd.action += ": " + det;
            }
            au.verdicts.push_back(std::move(vd));
        }
    }
    gModel.audits.push_back(std::move(au));
}

}  // namespace

// ---- Public: load a replayed run directory ---------------------------------

void loadInspectorRunDir(const std::string& absRunDir) {
    gModel = aicad::ui::AgentInspectorModel{};
    gModel.run_dir = absRunDir;
    gModel.state = InspectorState::Loading;

    const std::string eventsPath = absRunDir + "/events.jsonl";
    const std::string txt = readFile(eventsPath);
    if (txt.empty()) {
        gModel.state = InspectorState::Error;
        gModel.errorMsg = "no events.jsonl in run dir";
        return;
    }

    aicad::ui::InspectorTurn* cur = nullptr;
    std::istringstream in(txt);
    std::string line;
    int lineNo = 0;
    while (std::getline(in, line)) {
        ++lineNo;
        if (line.empty()) continue;
        json e;
        try { e = json::parse(line); } catch (...) { continue; }  // skip malformed line
        const std::string kind  = e.value("kind", std::string());
        const std::string actor = e.value("actor", std::string());
        const json pl = e.value("payload", json::object());

        if (kind == "run.started") {
            gModel.run_id = pl.value("run_id", std::string());
            if (pl.contains("brief")) {
                gModel.brief_title   = pl["brief"].value("title", std::string());
                gModel.brief_summary = pl["brief"].value("summary", std::string());
            }
        } else if (kind == "turn.start") {
            gModel.turns.emplace_back();
            cur = &gModel.turns.back();
            cur->turn  = pl.value("turn", (int)gModel.turns.size());
            cur->actor = actor;
            cur->stage = pl.value("stage", std::string());
            if (pl.contains("injected")) {
                const auto& inj = pl["injected"];
                cur->injected.ledger_render = inj.value("ledger_render", std::string());
                cur->injected.advisory      = inj.is_object() && inj.contains("advisory") && !inj["advisory"].is_null()
                                                  ? inj["advisory"].get<std::string>() : "";
                cur->injected.playbook_step = inj.value("playbook_step", std::string());
            }
        } else if (kind == "tool.call" && cur) {
            aicad::ui::InspectorToolCall tc;
            tc.tool         = pl.value("tool", std::string());
            tc.args_digest  = pl.value("args_digest", std::string());
            tc.preflight_id = pl.value("preflight", std::string());
            resolvePreflight(absRunDir, tc.preflight_id, tc.preflight_verdict, tc.preflight_detail);
            cur->tool_calls.push_back(std::move(tc));
        } else if (kind == "turn.end" && cur) {
            cur->status = pl.value("status", std::string());
            cur = nullptr;
        } else if (kind == "audit") {
            const std::string ref = pl.value("ref", std::string());
            if (!ref.empty()) {
                parseAudit(absRunDir, ref, cur ? cur->stage : std::string());
                if (cur) cur->audit_ref = ref;
            }
        } else if (kind == "failure") {
            const std::string ref = pl.value("ref", std::string());
            if (!ref.empty()) {
                parseFailure(absRunDir, ref);
                if (cur) cur->failure_ref = ref;
            }
        }
    }

    parseDelegation(absRunDir);

    if (gModel.turns.empty()) {
        gModel.state = InspectorState::Empty;
    } else {
        gModel.state = InspectorState::Ready;
        gModel.selectedTurn = 0;
    }
}

namespace {

// ---- filters ---------------------------------------------------------------

bool turnPassesFilters(const aicad::ui::InspectorTurn& t) {
    if (!gModel.filterActor.empty() && t.actor != gModel.filterActor) return false;
    if (!gModel.filterStage.empty() && t.stage != gModel.filterStage) return false;
    if (!gModel.filterSeverity.empty()) {
        // severity filter matches a turn that has any tool call with that
        // preflight verdict, or (for action/blocker) an advisory/failure/audit.
        const std::string& sv = gModel.filterSeverity;
        bool hit = false;
        for (const auto& tc : t.tool_calls)
            if (tc.preflight_verdict == sv) { hit = true; break; }
        if (!hit && (sv == "action" || sv == "warn") && !t.injected.advisory.empty()) hit = true;
        if (!hit && !t.failure_ref.empty() && (sv == "fail" || sv == "action")) hit = true;
        if (!hit) return false;
    }
    return true;
}

ImVec4 verdictColor(const std::string& v) {
    if (v == "fail")  return TVEC(bad);
    if (v == "warn")  return TVEC(warn);
    if (v == "pass")  return TVEC(good);
    return TVEC(txt2);
}

ImVec4 severityColor(const std::string& s) {
    if (s == "blocker") return TVEC(bad);
    if (s == "action")  return TVEC(warn);
    return TVEC(txt2);
}

// ---- filter chip row -------------------------------------------------------

void drawFilterChips() {
    const float S = theme::S();
    ImGui::Dummy(ImVec2(0, 2.f * S));
    ImGui::TextColored(TVEC(txt2), "Filter:");
    ImGui::SameLine();

    // Actor filter — cycle through distinct actors.
    auto chip = [&](const char* label, std::string& field, const char* value) {
        const bool active = (field == value);
        if (active) {
            ImGui::PushStyleColor(ImGuiCol_Button,        TVEC(accent));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered, TVEC(accent));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive,  TVEC(accent));
        }
        if (ImGui::SmallButton(label)) field = active ? "" : value;
        if (active) ImGui::PopStyleColor(3);
        ImGui::SameLine(0, 4.f * S);
    };

    chip("all", gModel.filterSeverity, "");  // reset severity via 'all'
    chip("warn", gModel.filterSeverity, "warn");
    chip("action", gModel.filterSeverity, "action");
    chip("fail", gModel.filterSeverity, "fail");
    ImGui::NewLine();
    ImGui::Dummy(ImVec2(0, 2.f * S));
}

// ---- turn timeline (left list) ---------------------------------------------

void drawTurnList() {
    const float S = theme::S();
    int shown = 0;
    for (int i = 0; i < (int)gModel.turns.size(); ++i) {
        const auto& t = gModel.turns[i];
        if (!turnPassesFilters(t)) continue;
        ++shown;
        const bool selected = (gModel.selectedTurn == i);
        ImGui::PushID(i);

        const ImVec2 rowMn = ImGui::GetCursorScreenPos();
        const float  rowH  = ImGui::GetTextLineHeightWithSpacing() * 2.f + 6.f * S;
        const ImVec2 rowMx(rowMn.x + ImGui::GetContentRegionAvail().x, rowMn.y + rowH);
        theme::glassRowBackground(ImGui::GetWindowDrawList(), rowMn, rowMx, 4.f * S, i, selected);

        ImGui::SetCursorScreenPos(rowMn);
        if (ImGui::InvisibleButton("##turn", ImVec2(rowMx.x - rowMn.x, rowH)))
            gModel.selectedTurn = i;
        ImGui::SetCursorScreenPos(ImVec2(rowMn.x + 8.f * S, rowMn.y + 3.f * S));

        ImGui::TextColored(TVEC(accent), "T%d", t.turn);
        ImGui::SameLine();
        ImGui::TextUnformatted(t.actor.c_str());
        if (!t.stage.empty()) { ImGui::SameLine(); ImGui::TextColored(TVEC(txt2), "· %s", t.stage.c_str()); }

        // second line: badges
        ImGui::SetCursorScreenPos(ImVec2(rowMn.x + 8.f * S, rowMn.y + ImGui::GetTextLineHeightWithSpacing() + 3.f * S));
        std::string worst;
        for (const auto& tc : t.tool_calls)
            if (tc.preflight_verdict == "fail") worst = "fail";
        if (worst.empty()) for (const auto& tc : t.tool_calls)
            if (tc.preflight_verdict == "warn") worst = "warn";
        std::string badges;
        if (!t.injected.advisory.empty()) badges += "advisory ";
        if (!t.failure_ref.empty())       badges += "failure ";
        if (!t.audit_ref.empty())         badges += "audit ";
        if (!worst.empty())               badges += "preflight:" + worst;
        if (badges.empty())               badges = std::to_string((int)t.tool_calls.size()) + " tool call(s)";
        ImGui::TextColored(worst == "fail" ? TVEC(bad) : TVEC(txt2), "%s", badges.c_str());

        ImGui::SetCursorScreenPos(ImVec2(rowMn.x, rowMx.y + 2.f * S));
        ImGui::PopID();
    }
    if (shown == 0)
        drawPanelEmptyState("No turns match the filter", "clear the severity filter");
}

// ---- verbatim block helper -------------------------------------------------

void drawVerbatim(const char* label, const std::string& text, ImVec4 col) {
    if (text.empty()) return;
    ImGui::TextColored(TVEC(txt1), "%s", label);
    ImGui::PushStyleColor(ImGuiCol_Text, col);
    ImGui::PushFont(theme::font(theme::Font::Mono));
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
    ImGui::TextUnformatted(text.c_str());
    ImGui::PopTextWrapPos();
    ImGui::PopFont();
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 4.f * theme::S()));
}

// ---- turn detail (right pane) ----------------------------------------------

void drawTurnDetail() {
    const float S = theme::S();
    if (gModel.selectedTurn < 0 || gModel.selectedTurn >= (int)gModel.turns.size()) {
        drawPanelEmptyState("Select a turn", "to see the verbatim injected context");
        return;
    }
    const auto& t = gModel.turns[gModel.selectedTurn];

    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::Text("Turn %d — %s", t.turn, t.actor.c_str());
    ImGui::PopFont();
    if (!t.stage.empty()) ImGui::TextColored(TVEC(txt2), "stage: %s   status: %s",
                                             t.stage.c_str(), t.status.c_str());
    ImGui::Dummy(ImVec2(0, 4.f * S));

    // Verbatim injected context — the troubleshooting core.
    drawVerbatim("Injected — ledger render:", t.injected.ledger_render, TVEC(txt1));
    drawVerbatim("Injected — advisory:", t.injected.advisory, TVEC(warn));
    drawVerbatim("Injected — playbook step:", t.injected.playbook_step, TVEC(txt2));

    // Tool calls + preflight verdicts.
    if (!t.tool_calls.empty()) {
        ImGui::TextColored(TVEC(txt1), "Tool calls:");
        if (ImGui::BeginTable("##tools", 3, ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_BordersInnerH)) {
            ImGui::TableSetupColumn("tool", ImGuiTableColumnFlags_WidthStretch, 0.30f);
            ImGui::TableSetupColumn("args", ImGuiTableColumnFlags_WidthStretch, 0.50f);
            ImGui::TableSetupColumn("preflight", ImGuiTableColumnFlags_WidthStretch, 0.20f);
            ImGui::TableHeadersRow();
            for (const auto& tc : t.tool_calls) {
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0); ImGui::TextUnformatted(tc.tool.c_str());
                ImGui::TableSetColumnIndex(1); ImGui::TextColored(TVEC(txt2), "%s", tc.args_digest.c_str());
                ImGui::TableSetColumnIndex(2);
                if (tc.preflight_verdict.empty()) ImGui::TextColored(TVEC(txt2), "—");
                else {
                    ImGui::TextColored(verdictColor(tc.preflight_verdict), "%s", tc.preflight_verdict.c_str());
                    if (!tc.preflight_detail.empty() && ImGui::IsItemHovered())
                        ImGui::SetTooltip("%s", tc.preflight_detail.c_str());
                }
            }
            ImGui::EndTable();
        }
        ImGui::Dummy(ImVec2(0, 4.f * S));
    }

    // Failure card (if this turn surfaced one).
    if (!t.failure_ref.empty()) {
        for (const auto& fc : gModel.failures) {
            const std::string want = "failures/" + fc.id + ".json";
            if (t.failure_ref != want) continue;
            ImGui::Separator();
            ImGui::TextColored(TVEC(bad), "Failure %s [%s]", fc.id.c_str(), fc.klass.c_str());
            ImGui::TextWrapped("%s", fc.what.c_str());
            if (!fc.why.empty()) ImGui::TextColored(TVEC(txt2), "why: %s", fc.why.c_str());
            for (const auto& a : fc.attempts)
                ImGui::TextColored(a.result == "improved" ? TVEC(good) : TVEC(txt2),
                                   "  attempt %d: %s → %s (%s)", a.n, a.fix_applied.c_str(),
                                   a.result.c_str(), a.delta.c_str());
            ImGui::Dummy(ImVec2(0, 4.f * S));
        }
    }

    // Audit verdicts (if this turn produced one).
    if (!t.audit_ref.empty()) {
        for (const auto& au : gModel.audits) {
            const std::string want = "audits/" + au.id + ".json";
            if (t.audit_ref != want) continue;
            ImGui::Separator();
            ImGui::TextColored(TVEC(txt1), "Audit %s (trigger: %s, %s)",
                               au.id.c_str(), au.trigger.c_str(), au.model.c_str());
            ImGui::TextColored(TVEC(txt2), "(review findings — not kernel facts)");
            for (const auto& v : au.verdicts) {
                ImGui::TextColored(severityColor(v.severity), "  [%s] %s: %s",
                                   v.severity.c_str(), v.klass.c_str(), v.finding.c_str());
                if (!v.action.empty() && v.action != "none")
                    ImGui::TextColored(TVEC(txt2), "     → %s", v.action.c_str());
            }
        }
    }
}

// ---- delegation tree tab ---------------------------------------------------

void drawDelegationTree() {
    const float S = theme::S();
    if (gModel.delegation.empty()) { drawPanelEmptyState("No delegation tree", "single-agent run"); return; }
    for (const auto& n : gModel.delegation) {
        ImGui::Indent((float)n.depth * 14.f * S);
        ImVec4 stCol = n.status == "failed" ? TVEC(bad)
                     : n.status == "running" ? TVEC(warn)
                     : n.status == "done" ? TVEC(good) : TVEC(txt2);
        ImGui::TextColored(TVEC(accent), "%s", n.role.c_str());
        ImGui::SameLine(); ImGui::TextColored(stCol, "[%s]", n.status.c_str());
        ImGui::SameLine(); ImGui::TextColored(TVEC(txt2), "%s", n.model.c_str());
        ImGui::TextColored(TVEC(txt2), "    %s", n.brief.c_str());
        ImGui::TextColored(TVEC(txt2), "    budget %d/%d tok · %d attempt(s) · approval: %s",
                           n.tokens_spent, n.tokens_granted, n.attempts, n.approval.c_str());
        ImGui::Unindent((float)n.depth * 14.f * S);
        ImGui::Dummy(ImVec2(0, 2.f * S));
    }
}

// ---- top-level content -----------------------------------------------------

void drawContent(app::AppState&) {
    switch (gModel.state) {
    case InspectorState::Idle:
        drawPanelEmptyState("No run loaded", "load a run directory to inspect");
        return;
    case InspectorState::Loading:
        drawPanelEmptyState("Loading run…");
        return;
    case InspectorState::Error:
        drawPanelEmptyState("Cannot load run", gModel.errorMsg.c_str());
        return;
    case InspectorState::Empty:
        // brief header + explicit empty state (run exists, no turns yet).
        if (!gModel.brief_title.empty())
            ImGui::TextColored(TVEC(txt2), "run %s — %s", gModel.run_id.c_str(), gModel.brief_title.c_str());
        drawPanelEmptyState("No turns recorded yet", "this run has not produced any agent turns");
        return;
    default: break;
    }

    // Ready: brief line + filters + tabs.
    ImGui::TextColored(TVEC(txt2), "run %s — %s", gModel.run_id.c_str(), gModel.brief_title.c_str());
    drawFilterChips();

    if (ImGui::BeginTabBar("##inspectorTabs")) {
        if (ImGui::BeginTabItem("Turns")) {
            // Turn list gets a modest slice; the detail pane (verbatim context,
            // preflight table, failure card + attempt history, audit verdicts)
            // gets the majority so a selected turn's full record is visible.
            const float listH = ImGui::GetContentRegionAvail().y * 0.32f;
            ImGui::BeginChild("##turnList", ImVec2(0, listH), ImGuiChildFlags_None);
            drawTurnList();
            ImGui::EndChild();
            ImGui::BeginChild("##turnDetail", ImVec2(0, 0), ImGuiChildFlags_None);
            drawTurnDetail();
            ImGui::EndChild();
            ImGui::EndTabItem();
        }
        ImGuiTabItemFlags delegFlags = gForceDelegationTab ? ImGuiTabItemFlags_SetSelected : 0;
        if (ImGui::BeginTabItem("Delegation", nullptr, delegFlags)) {
            gForceDelegationTab = false;  // one-shot: honor once, then let the user click freely
            ImGui::BeginChild("##deleg", ImVec2(0, 0), ImGuiChildFlags_None);
            drawDelegationTree();
            ImGui::EndChild();
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }
}

}  // namespace

// ---- Public API ------------------------------------------------------------

void drawAgentInspectorPanel(app::AppState& s) {
    ui::PanelScaffoldSpec spec;
    spec.id    = "agent_inspector";
    spec.title = "Agent Inspector";
    spec.icon  = "activity";
    spec.hasSearch = false;

    if (gScaffoldState.statusText.empty())
        gScaffoldState.statusText = "Replayed run — no live backend";
    gScaffoldState.statusText = gModel.state == InspectorState::Ready
        ? (std::to_string((int)gModel.turns.size()) + " turns · " +
           std::to_string((int)gModel.delegation.size()) + " delegation nodes")
        : gScaffoldState.statusText;

    ui::drawPanelScaffold(spec, gScaffoldState, [&]() { drawContent(s); });
}

void openAgentInspectorPanel() { gOpen = true; }
bool* agentInspectorPanelOpen() { return &gOpen; }

void inspectorShotSelectTurn(int turnIndex) {
    if (turnIndex >= 0 && turnIndex < (int)gModel.turns.size())
        gModel.selectedTurn = turnIndex;
}
void inspectorShotForceDelegationTab() { gForceDelegationTab = true; }

}  // namespace ui
