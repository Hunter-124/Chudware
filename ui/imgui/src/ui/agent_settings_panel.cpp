// T-W17 Agent Settings panel — see agent_settings_panel.h.
//
// Architecture:
//   - View-model: aicad::ui::AgentSettingsModel (ui/abstract/agent_settings_view.h)
//   - Scaffold: ui::drawPanelScaffold (T-U1 kit)
//   - Routing authority: services/agent/routing.py. The panel edits a STRUCTURED
//     model (never free text) and reads/writes it through the routing.py CLI:
//       python3 -m services.agent.routing get   -> a `view` JSON on stdout
//       python3 -m services.agent.routing set   <- a model JSON on stdin, writes
//                                                   routing.yaml, echoes the view
//     routing.py is the sole validator; the GUI never hand-emits YAML and never
//     re-implements resolve()/warnings (that logic lives in routing.py).
//   - Offline/shot mode seeds from a committed `view` JSON fixture — no backend.

#include "agent_settings_panel.h"
#include "panel_scaffold.h"
#include "agent_settings_view.h"   // ui/abstract/

#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/tokens.h"

#include "imgui.h"
#include <nlohmann/json.hpp>

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

namespace ui {

namespace {

bool gOpen = false;
ui::PanelScaffoldState gScaffoldState;
aicad::ui::AgentSettingsModel gModel;

using aicad::ui::SettingsState;
using nlohmann::json;

// ---- routing.py CLI seam ---------------------------------------------------

std::string envOr(const char* key, const char* dflt) {
    const char* v = std::getenv(key);
    return (v && *v) ? std::string(v) : std::string(dflt);
}

// POSIX single-quote for the shell.
std::string shq(const std::string& s) {
    std::string out = "'";
    for (char c : s) { if (c == '\'') out += "'\\''"; else out += c; }
    out += "'"; return out;
}

// Run `python3 -m services.agent.routing <sub>` in the repo root. `stdinText`
// (if non-empty) is fed on stdin. Returns stdout; sets ok. No-op-safe: if no
// repo/python is discoverable this returns ok=false and the caller keeps the
// current model (offline/shot mode is unaffected — it never calls this).
std::string runRoutingCli(const std::string& sub, const std::string& stdinText, bool& ok) {
    ok = false;
#if defined(_WIN32)
    (void)sub; (void)stdinText; return {};   // wired on POSIX; Windows uses worker path (future)
#else
    const std::string py   = envOr("AI_CAD_PYTHON", "python3");
    const std::string repo = envOr("AI_CAD_REPO", ".");
    std::string cmd = "cd " + shq(repo) + " && " + shq(py) +
                      " -m services.agent.routing " + sub + " 2>/dev/null";
    if (!stdinText.empty()) {
        // Feed stdin via a here-doc-free approach: printf the JSON then pipe.
        cmd = "printf '%s' " + shq(stdinText) + " | " + cmd;
    }
    FILE* p = popen(cmd.c_str(), "r");
    if (!p) return {};
    std::string out;
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), p)) > 0) out.append(buf, n);
    int rc = pclose(p);
    ok = (rc == 0) && !out.empty();
    return out;
#endif
}

// ---- view JSON -> model ----------------------------------------------------

void applyViewJson(const json& v) {
    gModel.sources.clear();
    gModel.assignments.clear();
    if (v.contains("sources") && v["sources"].is_array()) {
        for (const auto& s : v["sources"]) {
            aicad::ui::SettingsSource src;
            src.id   = s.value("id", std::string());
            src.kind = s.value("kind", std::string());
            if (s.contains("models") && s["models"].is_array())
                for (const auto& m : s["models"])
                    src.models.push_back({m.value("id", std::string()), m.value("tier", std::string())});
            gModel.sources.push_back(std::move(src));
        }
    }
    const json& assigns  = v.value("assignments", json::object());
    const json& resolved = v.value("resolved", json::object());
    // Keep overseer first and default last for a stable, readable order.
    std::vector<std::string> order;
    for (auto it = assigns.begin(); it != assigns.end(); ++it)
        if (it.key() != "default") order.push_back(it.key());
    order.push_back("default");
    // overseer to the front if present
    for (size_t i = 0; i < order.size(); ++i)
        if (order[i] == "overseer") { std::swap(order[0], order[i]); break; }

    for (const auto& role : order) {
        if (!assigns.contains(role)) continue;
        aicad::ui::SettingsAssignment a;
        a.role = role;
        for (const auto& tok : assigns[role]) a.prefs.push_back(tok.get<std::string>());
        if (resolved.contains(role)) {
            const auto& r = resolved[role];
            if (!r["model"].is_null())  a.resolved_model  = r["model"].get<std::string>();
            if (!r["source"].is_null()) a.resolved_source = r["source"].get<std::string>();
            if (r.contains("warnings"))
                for (const auto& w : r["warnings"]) a.warnings.push_back(w.get<std::string>());
        }
        gModel.assignments.push_back(std::move(a));
    }
    if (v.contains("approval_policy")) {
        gModel.approval_depth1 = v["approval_policy"].value("depth_1", gModel.approval_depth1);
        gModel.approval_deeper = v["approval_policy"].value("deeper", gModel.approval_deeper);
    }
    gModel.validationError = v.value("validation_error", std::string());
    gModel.state = SettingsState::Ready;
}

// ---- model -> routing set JSON (structured, never free text) ---------------

json modelToRoutingJson() {
    json doc;
    doc["schema"] = "aicad.routing/1";
    doc["sources"] = json::array();
    for (const auto& s : gModel.sources) {
        json js; js["id"] = s.id; js["kind"] = s.kind; js["models"] = json::array();
        for (const auto& m : s.models) js["models"].push_back({{"id", m.id}, {"tier", m.tier}});
        doc["sources"].push_back(js);
    }
    doc["assignments"] = json::object();
    for (const auto& a : gModel.assignments) doc["assignments"][a.role] = a.prefs;
    doc["approval_policy"] = {{"depth_1", gModel.approval_depth1}, {"deeper", gModel.approval_deeper}};
    return doc;
}

// Refresh via CLI get (used on open when a backend is available).
void refreshFromCli() {
    gModel.state = SettingsState::Loading;
    bool ok = false;
    const std::string out = runRoutingCli("get", "", ok);
    if (!ok) { gModel.state = SettingsState::Error; gModel.errorMsg = "routing.py get unavailable (offline)"; return; }
    try { applyViewJson(json::parse(out)); }
    catch (...) { gModel.state = SettingsState::Error; gModel.errorMsg = "could not parse routing view"; }
}

// Write via CLI set — routing.py validates; the echoed view refreshes the model
// (including any validation_error, shown inline). This is the "through routing.py
// validation, never raw text" path.
void saveThroughRouting() {
    bool ok = false;
    const std::string payload = modelToRoutingJson().dump();
    const std::string out = runRoutingCli("set", payload, ok);
    if (out.empty()) {
        gModel.statusText = "routing.py set unavailable (offline) — edits not persisted";
        return;
    }
    try {
        const json v = json::parse(out);
        if (v.contains("error") || (v.contains("ok") && v["ok"].is_boolean() && !v["ok"].get<bool>())) {
            gModel.validationError = v.value("error", std::string("validation failed"));
            gModel.statusText = "not saved — validation error";
            return;
        }
        applyViewJson(v);
        gModel.validationError.clear();
        gModel.statusText = "saved — routing.yaml updated through validation";
    } catch (...) {
        gModel.statusText = "unexpected routing.py output";
    }
}

// ---- rendering -------------------------------------------------------------

// One editable assignment row: role name, drag-order prefs, resolved readout, warnings.
void drawAssignmentRow(int idx) {
    const float S = theme::S();
    auto& a = gModel.assignments[idx];
    ImGui::PushID(idx);

    ImGui::TableNextRow();
    // Col 0: role
    ImGui::TableSetColumnIndex(0);
    ImGui::TextColored(a.role == "overseer" ? TVEC(accent) : TVEC(txt0), "%s", a.role.c_str());

    // Col 1: ordered preference tokens with up/down reorder + remove.
    ImGui::TableSetColumnIndex(1);
    for (int t = 0; t < (int)a.prefs.size(); ++t) {
        ImGui::PushID(t);
        ImGui::TextColored(TVEC(txt1), "%d. %s", t + 1, a.prefs[t].c_str());
        ImGui::SameLine();
        if (ImGui::SmallButton("^") && t > 0) std::swap(a.prefs[t], a.prefs[t - 1]);
        ImGui::SameLine();
        if (ImGui::SmallButton("v") && t < (int)a.prefs.size() - 1) std::swap(a.prefs[t], a.prefs[t + 1]);
        ImGui::SameLine();
        if (ImGui::SmallButton("x") && a.prefs.size() > 1) { a.prefs.erase(a.prefs.begin() + t); ImGui::PopID(); break; }
        ImGui::PopID();
    }
    // Add-token combo (symbolic tokens).
    if (ImGui::SmallButton("+ token")) ImGui::OpenPopup("addtok");
    if (ImGui::BeginPopup("addtok")) {
        for (const auto& tok : aicad::ui::settingsSymbolicTokens())
            if (ImGui::Selectable(tok.c_str())) a.prefs.push_back(tok);
        ImGui::EndPopup();
    }

    // Col 2: live "resolves to" readout.
    ImGui::TableSetColumnIndex(2);
    if (a.resolved_model.empty())
        ImGui::TextColored(TVEC(bad), "(unmet)");
    else
        ImGui::TextColored(TVEC(good), "%s@%s", a.resolved_model.c_str(), a.resolved_source.c_str());
    // Warnings (warn, never block).
    for (const auto& w : a.warnings) {
        ImGui::TextColored(TVEC(warn), "! ");
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", w.c_str());
        ImGui::SameLine();
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + 260.f * S);
        ImGui::TextColored(TVEC(warn), "%s", w.c_str());
        ImGui::PopTextWrapPos();
    }
    ImGui::PopID();
}

void drawSourcesSection() {
    const float S = theme::S();
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextUnformatted("Model sources");
    ImGui::PopFont();
    ImGui::TextColored(TVEC(txt2), "Kinds: anthropic / openai / openrouter / lmstudio / llamacpp / ollama (no consumer-OAuth)");

    for (int i = 0; i < (int)gModel.sources.size(); ++i) {
        auto& src = gModel.sources[i];
        ImGui::PushID(1000 + i);
        ImGui::BulletText("%s", src.id.c_str());
        ImGui::SameLine(); ImGui::TextColored(TVEC(accent), "[%s]", src.kind.c_str());
        ImGui::SameLine();
        if (ImGui::SmallButton("remove") && gModel.sources.size() > 1)
            { gModel.sources.erase(gModel.sources.begin() + i); ImGui::PopID(); break; }
        for (const auto& m : src.models)
            ImGui::TextColored(TVEC(txt2), "    %s (%s)", m.id.c_str(), m.tier.c_str());
        ImGui::PopID();
    }
    if (ImGui::SmallButton("+ add source (edit routing.yaml via routing.py)"))
        gModel.statusText = "add sources through the validated form (routing.py set)";
    ImGui::Dummy(ImVec2(0, 6.f * S));
}

void drawTogglesSection() {
    const float S = theme::S();
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextUnformatted("Policy & toggles");
    ImGui::PopFont();

    // Approval policy.
    const char* pol[] = {"auto", "overseer", "human"};
    auto combo = [&](const char* label, std::string& val) {
        int cur = 0; for (int i = 0; i < 3; ++i) if (val == pol[i]) cur = i;
        if (ImGui::BeginCombo(label, pol[cur])) {
            for (int i = 0; i < 3; ++i) if (ImGui::Selectable(pol[i], cur == i)) val = pol[i];
            ImGui::EndCombo();
        }
    };
    ImGui::SetNextItemWidth(180.f * S); combo("delegation depth-1", gModel.approval_depth1);
    ImGui::SetNextItemWidth(180.f * S); combo("delegation deeper",  gModel.approval_deeper);

    ImGui::Checkbox("Auto-coincident", &gModel.auto_coincident);
    ImGui::Checkbox("Preflight enabled", &gModel.preflight_enabled);
    const char* verb[] = {"quiet", "normal", "verbose"};
    int vc = 1; for (int i = 0; i < 3; ++i) if (gModel.advisory_verbosity == verb[i]) vc = i;
    ImGui::SetNextItemWidth(180.f * S);
    if (ImGui::BeginCombo("Advisory verbosity", verb[vc])) {
        for (int i = 0; i < 3; ++i) if (ImGui::Selectable(verb[i], vc == i)) gModel.advisory_verbosity = verb[i];
        ImGui::EndCombo();
    }
    ImGui::Dummy(ImVec2(0, 6.f * S));
}

void drawContent(app::AppState&) {
    const float S = theme::S();
    if (gModel.state == SettingsState::Idle) {
        drawPanelEmptyState("Loading routing…", "(reads services/agent/routing.yaml via routing.py)");
        return;
    }
    if (gModel.state == SettingsState::Loading) { drawPanelEmptyState("Loading routing…"); return; }
    if (gModel.state == SettingsState::Error) {
        drawPanelEmptyState("Routing unavailable", gModel.errorMsg.c_str());
        return;
    }

    // Inline validation banner (never blocks editing).
    if (!gModel.validationError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(bad));
        ImGui::TextWrapped("Validation: %s", gModel.validationError.c_str());
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 4.f * S));
    }

    drawSourcesSection();

    // Per-role assignment table — every role incl. overseer + default.
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextUnformatted("Per-role model assignment");
    ImGui::PopFont();
    ImGui::TextColored(TVEC(txt2), "Ordered preference; first available wins. 'Resolves to' shows the current consequence.");
    if (ImGui::BeginTable("##assign", 3,
            ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_RowBg)) {
        ImGui::TableSetupColumn("Role",        ImGuiTableColumnFlags_WidthStretch, 0.22f);
        ImGui::TableSetupColumn("Preference",  ImGuiTableColumnFlags_WidthStretch, 0.44f);
        ImGui::TableSetupColumn("Resolves to", ImGuiTableColumnFlags_WidthStretch, 0.34f);
        ImGui::TableHeadersRow();
        for (int i = 0; i < (int)gModel.assignments.size(); ++i) drawAssignmentRow(i);
        ImGui::EndTable();
    }
    ImGui::Dummy(ImVec2(0, 6.f * S));

    drawTogglesSection();

    // Save through routing.py validation.
    if (ImGui::Button("Save (validate + write routing.yaml)", ImVec2(-1, 0)))
        saveThroughRouting();
}

}  // namespace

// ---- Public API ------------------------------------------------------------

void drawAgentSettingsPanel(app::AppState& s) {
    ui::PanelScaffoldSpec spec;
    spec.id    = "agent_settings";
    spec.title = "Agent Settings";
    spec.icon  = "settings";
    spec.hasSearch = false;

    // Lazily load from the CLI on first open when nothing has been seeded (a shot
    // seeds before first draw, so this only fires in the live app).
    if (gModel.state == SettingsState::Idle) refreshFromCli();

    gScaffoldState.statusText = !gModel.statusText.empty()
        ? gModel.statusText
        : "Edits write routing.yaml through routing.py validation";
    gScaffoldState.statusIsError = !gModel.validationError.empty();

    ui::drawPanelScaffold(spec, gScaffoldState, [&]() { drawContent(s); });
}

void openAgentSettingsPanel() { gOpen = true; }
bool* agentSettingsPanelOpen() { return &gOpen; }

void seedAgentSettingsFromViewJson(const std::string& view_json) {
    try { applyViewJson(json::parse(view_json)); }
    catch (...) { gModel.state = SettingsState::Error; gModel.errorMsg = "malformed routing view fixture"; }
}

}  // namespace ui
