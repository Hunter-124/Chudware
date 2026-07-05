// T-W28 "AI Effort" settings section — see ai_effort_section.h.
//
// Architecture (mirrors the T-W17 agent settings panel):
//   - View-model: aicad::ui::AiEffortModel (ui/abstract/ai_effort_view.h)
//   - Authority: services/agent/effort.py — the panel edits a STRUCTURED model
//     and reads/writes through the effort CLI:
//       python3 -m services.agent.effort get  -> `view` JSON on stdout
//       python3 -m services.agent.effort set  <- {"level","mode","overrides"}
//                                                on stdin, persists to
//                                                config.json "ai_effort",
//                                                echoes the fresh view
//     effort.py owns resolution + warnings (warn-never-block, contract §23);
//     the GUI never re-implements either.
//   - Offline/shot mode seeds from a committed view fixture — no backend.

#include "ai_effort_section.h"
#include "ai_effort_view.h"   // ui/abstract/
#include "model_picker.h"     // drawSettingsSectionHeader

#include "app/app_state.h"
#include "platform/run_subprocess.h"
#include "theme/theme.h"
#include "theme/tokens.h"
#include "theme/domain_tokens.h"

#include "imgui.h"
#include <nlohmann/json.hpp>

#include <algorithm>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

namespace ui {

namespace {

aicad::ui::AiEffortModel gModel;

using aicad::ui::EffortState;
using nlohmann::json;

// ---- effort.py CLI seam -----------------------------------------------------

std::string envOr(const char* key, const char* dflt) {
    const char* v = std::getenv(key);
    return (v && *v) ? std::string(v) : std::string(dflt);
}

std::string runEffortCli(const std::string& sub, const std::string& stdinText, bool& ok) {
    const std::string py   = envOr("AI_CAD_PYTHON", "python3");
    const std::string repo = envOr("AI_CAD_REPO", ".");
    const std::string cmd = "cd " + platform::shellQuote(repo) + " && " +
                            platform::shellQuote(py) +
                            " -m services.agent.effort " + sub + " 2>/dev/null";
    return platform::runShellCapture(cmd, stdinText, ok);
}

// ---- view JSON -> model -----------------------------------------------------

void applyViewJson(const json& v) {
    gModel.levels.clear();
    gModel.knobMeta.clear();
    gModel.tiers.clear();

    gModel.level = v.value("level", std::string("standard"));
    gModel.mode  = v.value("mode", std::string("simple"));

    for (const auto& lv : v.value("levels", json::array())) {
        aicad::ui::EffortLevel L;
        L.id    = lv.value("id", std::string());
        L.label = lv.value("label", std::string());
        L.hint  = lv.value("hint", std::string());
        gModel.levels.push_back(std::move(L));
    }

    // Knob catalog, ordered by group so the advanced layout is stable.
    const json& knobs = v.value("knobs", json::object());
    std::vector<std::string> groupOrder;
    for (const auto& g : v.value("groups", json::array()))
        groupOrder.push_back(g.get<std::string>());
    for (const auto& grp : groupOrder) {
        for (auto it = knobs.begin(); it != knobs.end(); ++it) {
            if (it.value().value("group", std::string()) != grp) continue;
            aicad::ui::EffortKnobMeta m;
            m.name  = it.key();
            m.group = grp;
            m.type  = it.value().value("type", std::string());
            m.label = it.value().value("label", it.key());
            m.doc   = it.value().value("doc", std::string());
            for (const auto& c : it.value().value("choices", json::array()))
                m.choices.push_back(c.get<std::string>());
            gModel.knobMeta.push_back(std::move(m));
        }
    }

    // Tiers in the canonical dial order when present.
    const json& tiers = v.value("tiers", json::object());
    static const char* order[] = {"local-24k", "local-48k", "mid", "frontier"};
    std::vector<std::string> tnames;
    for (const char* t : order)
        if (tiers.contains(t)) tnames.push_back(t);
    for (auto it = tiers.begin(); it != tiers.end(); ++it)
        if (std::find(tnames.begin(), tnames.end(), it.key()) == tnames.end())
            tnames.push_back(it.key());

    for (const auto& tn : tnames) {
        const json& tp = tiers[tn];
        aicad::ui::EffortTier T;
        T.tier = tn;
        const json& vals = tp.value("values", json::object());
        const json& srcs = tp.value("sources", json::object());
        for (const auto& m : gModel.knobMeta) {
            if (!vals.contains(m.name)) continue;
            aicad::ui::EffortKnobValue kv;
            kv.name   = m.name;
            kv.source = srcs.value(m.name, std::string("shipped"));
            const json& jv = vals[m.name];
            if (m.type == "enum")        kv.sval = jv.is_string() ? jv.get<std::string>() : "";
            else if (m.type == "bool")   kv.bval = jv.is_boolean() && jv.get<bool>();
            else if (m.type == "int_map") {
                if (jv.is_object())
                    for (auto mit = jv.begin(); mit != jv.end(); ++mit)
                        kv.mapval.push_back({mit.key(), mit.value().is_number()
                                                            ? mit.value().get<int>() : 0});
                std::sort(kv.mapval.begin(), kv.mapval.end());
            } else                        kv.nval = jv.is_number() ? jv.get<double>() : 0.0;
            T.knobs.push_back(std::move(kv));
        }
        for (const auto& w : tp.value("warnings", json::array()))
            T.warnings.push_back(w.get<std::string>());
        gModel.tiers.push_back(std::move(T));
    }
    if (gModel.selectedTier >= (int)gModel.tiers.size()) gModel.selectedTier = 0;
    gModel.state = EffortState::Ready;
}

// ---- model -> set JSON (structured; overrides reconstructed from provenance) --

json knobValueToJson(const aicad::ui::EffortKnobMeta& m, const aicad::ui::EffortKnobValue& kv) {
    if (m.type == "enum") return kv.sval;
    if (m.type == "bool") return kv.bval;
    if (m.type == "int")  return (long long)(kv.nval + (kv.nval >= 0 ? 0.5 : -0.5));
    if (m.type == "float") return kv.nval;
    json o = json::object();
    for (const auto& [k, a] : kv.mapval) o[k] = a;
    return o;
}

const aicad::ui::EffortKnobMeta* metaFor(const std::string& name) {
    for (const auto& m : gModel.knobMeta)
        if (m.name == name) return &m;
    return nullptr;
}

// The user override block IS "every knob whose source is 'override'" — the view
// carries provenance precisely so the GUI can reconstruct it without shadowing
// effort.py's resolution.
json modelToSetJson() {
    json doc;
    doc["level"] = gModel.level;
    doc["mode"]  = gModel.mode;
    json ov = json::object();
    for (const auto& t : gModel.tiers) {
        json tov = json::object();
        for (const auto& kv : t.knobs) {
            if (kv.source != "override") continue;
            if (const auto* m = metaFor(kv.name)) tov[kv.name] = knobValueToJson(*m, kv);
        }
        if (!tov.empty()) ov[t.tier] = tov;
    }
    doc["overrides"] = ov;
    return doc;
}

void refreshFromCli() {
    gModel.state = EffortState::Loading;
    bool ok = false;
    const std::string out = runEffortCli("get", "", ok);
    if (!ok || out.empty()) {
        gModel.state = EffortState::Error;
        gModel.errorMsg = "effort settings unavailable (effort.py CLI not reachable)";
        return;
    }
    try { applyViewJson(json::parse(out)); }
    catch (...) { gModel.state = EffortState::Error; gModel.errorMsg = "could not parse effort view"; }
}

// Persist through effort.py (the sole validator); echoed view refreshes the
// model including provenance + warnings. Warnings NEVER veto the save.
void saveThroughEffort() {
    bool ok = false;
    const std::string out = runEffortCli("set", modelToSetJson().dump(), ok);
    if (out.empty()) {
        gModel.statusText = "effort.py set unavailable (offline) — edits not persisted";
        return;
    }
    try {
        const json v = json::parse(out);
        if (v.contains("ok") && v["ok"].is_boolean() && !v["ok"].get<bool>()) {
            gModel.statusText = "not saved — " + v.value("error", std::string("invalid effort config"));
            return;
        }
        applyViewJson(v);
        gModel.statusText = "saved — config.json ai_effort updated";
    } catch (...) {
        gModel.statusText = "unexpected effort.py output";
    }
}

// ---- edit helpers -------------------------------------------------------------

// Mark a knob as user-overridden with its current model value and persist.
void commitOverride(aicad::ui::EffortKnobValue& kv) {
    kv.source = "override";
    saveThroughEffort();
}

// Drop a user override (revert to preset/shipped) and persist.
void resetOverride(aicad::ui::EffortKnobValue& kv) {
    kv.source = "shipped";  // any non-"override" value drops it from the set JSON
    saveThroughEffort();
}

// ---- rendering ------------------------------------------------------------------

void drawWarnings(const std::vector<std::string>& warnings) {
    const float S = theme::S();
    for (const auto& w : warnings) {
        ImGui::TextColored(TVEC(warn), "! ");
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", w.c_str());
        ImGui::SameLine();
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + 420.f * S);
        ImGui::TextColored(TVEC(warn), "%s", w.c_str());
        ImGui::PopTextWrapPos();
    }
}

void drawSourceTag(const aicad::ui::EffortKnobValue& kv) {
    ImGui::SameLine();
    if (kv.source == "override")
        ImGui::TextColored(TVEC(accent), "[override]");
    else
        ImGui::TextColored(TVEC(txt2), "[%s]", kv.source.c_str());
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Which layer supplies this value:\nshipped defaults < preset < your override");
}

void drawSimpleMode() {
    const float S = theme::S();
    ImGui::TextColored(TVEC(txt2), "One dial for how hard the AI harness works. Each level is a preset over every knob.");
    ImGui::Dummy(ImVec2(0, 4.f * S));

    // Segmented 4-level control.
    const std::string* activeHint = nullptr;
    for (int i = 0; i < (int)gModel.levels.size(); ++i) {
        const auto& lv = gModel.levels[i];
        const bool active = (lv.id == gModel.level);
        if (i > 0) ImGui::SameLine();
        if (active) {
            const auto& p = theme::pal();
            ImGui::PushStyleColor(ImGuiCol_Button, theme::vec(p.accent));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered, theme::vec(p.accent));
            ImGui::PushStyleColor(ImGuiCol_ButtonActive, theme::vec(p.accentDim));
            ImGui::PushStyleColor(ImGuiCol_Text, theme::panelview::accentButtonInk());
        }
        if (ImGui::Button(lv.label.c_str(), ImVec2(105.f * S, 30.f * S)) && !active) {
            gModel.level = lv.id;
            saveThroughEffort();
        }
        if (active) ImGui::PopStyleColor(4);
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", lv.hint.c_str());
        if (active) activeHint = &lv.hint;
    }
    if (activeHint) {
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + 520.f * S);
        ImGui::TextColored(TVEC(txt2), "%s", activeHint->c_str());
        ImGui::PopTextWrapPos();
    }

    // Aggregate warnings across tiers (rare in simple mode, but overrides made
    // in advanced mode persist and may warn).
    for (const auto& t : gModel.tiers) {
        if (t.warnings.empty()) continue;
        ImGui::TextColored(TVEC(txt2), "%s:", t.tier.c_str());
        drawWarnings(t.warnings);
    }
}

void drawKnobControl(const aicad::ui::EffortKnobMeta& m, aicad::ui::EffortKnobValue& kv) {
    const float S = theme::S();
    ImGui::PushID(m.name.c_str());

    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(m.label.c_str());
    if (ImGui::IsItemHovered() && !m.doc.empty()) ImGui::SetTooltip("%s", m.doc.c_str());
    ImGui::SameLine(230.f * S);

    if (m.type == "enum") {
        ImGui::SetNextItemWidth(160.f * S);
        if (ImGui::BeginCombo("##v", kv.sval.c_str())) {
            for (const auto& c : m.choices)
                if (ImGui::Selectable(c.c_str(), c == kv.sval) && c != kv.sval) {
                    kv.sval = c;
                    commitOverride(kv);
                }
            ImGui::EndCombo();
        }
    } else if (m.type == "bool") {
        bool b = kv.bval;
        if (ImGui::Checkbox("##v", &b)) {
            kv.bval = b;
            commitOverride(kv);
        }
    } else if (m.type == "int") {
        int iv = (int)(kv.nval + (kv.nval >= 0 ? 0.5 : -0.5));
        ImGui::SetNextItemWidth(120.f * S);
        ImGui::InputInt("##v", &iv);
        if (ImGui::IsItemDeactivatedAfterEdit() &&
            iv != (int)(kv.nval + (kv.nval >= 0 ? 0.5 : -0.5))) {
            kv.nval = iv;   // no clamping — freedom rule; effort.py warns instead
            commitOverride(kv);
        }
    } else if (m.type == "float") {
        float f = (float)kv.nval;
        ImGui::SetNextItemWidth(120.f * S);
        ImGui::InputFloat("##v", &f, 0.f, 0.f, "%.2f");
        if (ImGui::IsItemDeactivatedAfterEdit() && f != (float)kv.nval) {
            kv.nval = f;
            commitOverride(kv);
        }
    } else if (m.type == "int_map") {
        drawSourceTag(kv);
        if (kv.source == "override") {
            ImGui::SameLine();
            if (ImGui::SmallButton("reset")) resetOverride(kv);
        }
        ImGui::Indent(24.f * S);
        bool edited = false;
        for (auto& [cls, attempts] : kv.mapval) {
            ImGui::PushID(cls.c_str());
            ImGui::AlignTextToFramePadding();
            ImGui::TextColored(TVEC(txt1), "%s", cls.c_str());
            ImGui::SameLine(150.f * S);
            ImGui::SetNextItemWidth(90.f * S);
            ImGui::InputInt("##a", &attempts);
            if (ImGui::IsItemDeactivatedAfterEdit()) edited = true;
            ImGui::PopID();
        }
        ImGui::Unindent(24.f * S);
        if (edited) commitOverride(kv);
        ImGui::PopID();
        return;  // int_map draws its own tag above
    }

    drawSourceTag(kv);
    if (kv.source == "override") {
        ImGui::SameLine();
        if (ImGui::SmallButton("reset")) resetOverride(kv);
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Drop your override — back to the preset / shipped value");
    }
    ImGui::PopID();
}

void drawAdvancedMode() {
    const float S = theme::S();
    if (gModel.tiers.empty()) return;

    ImGui::TextColored(TVEC(txt2),
        "All harness knobs, per model tier. Values fall through: shipped defaults < preset < your overrides.");
    ImGui::Dummy(ImVec2(0, 4.f * S));

    // Tier selector.
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted("Model tier");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(160.f * S);
    if (ImGui::BeginCombo("##tier", gModel.tiers[gModel.selectedTier].tier.c_str())) {
        for (int i = 0; i < (int)gModel.tiers.size(); ++i)
            if (ImGui::Selectable(gModel.tiers[i].tier.c_str(), i == gModel.selectedTier))
                gModel.selectedTier = i;
        ImGui::EndCombo();
    }
    ImGui::SameLine();
    ImGui::TextColored(TVEC(txt2), "(shipped dial is inverted: local = full machinery, frontier = minimal)");

    auto& tier = gModel.tiers[gModel.selectedTier];

    struct GroupTitle { const char* key; const char* title; };
    static const GroupTitle groups[] = {
        {"deliberation",    "Deliberation"},
        {"parallelization", "Parallelization"},
        {"safety",          "Safety nets"},
        {"context",         "Context"},
    };
    for (const auto& g : groups) {
        if (!ImGui::CollapsingHeader(g.title, ImGuiTreeNodeFlags_DefaultOpen)) continue;
        ImGui::Indent(8.f * S);
        for (const auto& m : gModel.knobMeta) {
            if (m.group != g.key) continue;
            for (auto& kv : tier.knobs)
                if (kv.name == m.name) { drawKnobControl(m, kv); break; }
        }
        ImGui::Unindent(8.f * S);
        ImGui::Dummy(ImVec2(0, 2.f * S));
    }

    if (!tier.warnings.empty()) {
        ImGui::Dummy(ImVec2(0, 2.f * S));
        drawWarnings(tier.warnings);
    }
}

}  // namespace

// ---- Public API ---------------------------------------------------------------

void drawAiEffortSection(app::AppState&) {
    const float S = theme::S();
    drawSettingsSectionHeader("AI Effort");

    // Shot mode: the section lives at the bottom of the AI tab — scroll it into
    // view so the ai_effort_* captures actually show it (no chrome changes).
    static const bool s_shotFocus = [] {
        const char* s = std::getenv("AI_CAD_SHOT");
        return s && std::strncmp(s, "ai_effort", 9) == 0;
    }();
    if (s_shotFocus) ImGui::SetScrollHereY(0.02f);

    // Lazy first load (shots seed before first draw, so this only fires live).
    if (gModel.state == EffortState::Idle) refreshFromCli();

    if (gModel.state == EffortState::Loading) {
        ImGui::TextColored(TVEC(txt2), "Loading effort settings…");
        return;
    }
    if (gModel.state == EffortState::Error) {
        ImGui::TextColored(TVEC(txt2), "%s", gModel.errorMsg.c_str());
        return;
    }

    drawSimpleMode();
    ImGui::Dummy(ImVec2(0, 4.f * S));

    bool advanced = (gModel.mode == "advanced");
    if (ImGui::Checkbox("Advanced mode (all harness knobs)", &advanced)) {
        gModel.mode = advanced ? "advanced" : "simple";
        saveThroughEffort();
    }
    if (advanced) {
        ImGui::Dummy(ImVec2(0, 2.f * S));
        drawAdvancedMode();
    }

    if (!gModel.statusText.empty())
        ImGui::TextColored(TVEC(txt2), "%s", gModel.statusText.c_str());
}

void seedAiEffortFromViewJson(const std::string& view_json) {
    try { applyViewJson(json::parse(view_json)); }
    catch (...) {
        gModel.state = EffortState::Error;
        gModel.errorMsg = "malformed effort view fixture";
    }
}

}  // namespace ui
