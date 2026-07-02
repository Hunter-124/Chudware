// In-app-dialog transport (contracts/interaction.md §8) -- PropertyManager-style modal
// built from an ask_user batch (ui/abstract/ask_form.h parses/serializes; this file only
// renders + collects). Transports stay dumb (§8/§10): no normalization/persistence here.
#include "ask_dialog.h"

#include "ask_form.h"

#include "app/app_state.h"

#include "imgui.h"

#include <nlohmann/json.hpp>

#include <array>
#include <cstdio>
#include <map>
#include <vector>

namespace ui {

namespace {

using aicad::ui::AskField;
using aicad::ui::AskFieldKind;
using aicad::ui::AskForm;

constexpr const char* kLengthUnits[] = {"mm", "cm", "m", "in", "ft"};
constexpr const char* kAngleUnits[]  = {"deg"};
constexpr const char* kCountUnits[]  = {"count"};

void unitsForFamily(const std::string& family, const char* const*& units, int& count) {
    if (family == "angle")      { units = kAngleUnits; count = 1; }
    else if (family == "count") { units = kCountUnits; count = 1; }
    else                        { units = kLengthUnits; count = 5; }
}

struct FieldState {
    double dimValue = 0.0;
    int    dimUnitIdx = 0;
    int    choiceIdx = 0;
    bool   approved = false;
    std::array<char, 256> textBuf{};
};

// Prefill widget state from a field's typed `suggested` JSON (contract §2).
void initFieldState(FieldState& fs, const AskField& f) {
    nlohmann::json sug = nlohmann::json::parse(f.suggestedJson, nullptr, false);
    fs = FieldState{};
    switch (f.kind) {
    case AskFieldKind::Dimension: {
        if (!sug.is_discarded() && sug.is_object()) {
            auto vit = sug.find("value");
            if (vit != sug.end() && vit->is_number()) fs.dimValue = vit->get<double>();
            std::string unit = sug.value("unit", std::string());
            const char* const* units; int n;
            unitsForFamily(f.family, units, n);
            for (int i = 0; i < n; ++i) if (unit == units[i]) { fs.dimUnitIdx = i; break; }
        }
        break;
    }
    case AskFieldKind::Choice: {
        std::string sv = (!sug.is_discarded() && sug.is_string()) ? sug.get<std::string>() : std::string();
        for (size_t i = 0; i < f.options.size(); ++i)
            if (f.options[i] == sv) { fs.choiceIdx = (int)i; break; }
        break;
    }
    case AskFieldKind::Text: {
        std::string sv = (!sug.is_discarded() && sug.is_string()) ? sug.get<std::string>() : std::string();
        std::snprintf(fs.textBuf.data(), fs.textBuf.size(), "%s", sv.c_str());
        break;
    }
    case AskFieldKind::FileRequest:
        break;  // no typed default; user types/pastes a path
    case AskFieldKind::Approval:
        fs.approved = (!sug.is_discarded() && sug.is_boolean()) ? sug.get<bool>() : false;
        break;
    }
}

struct DialogState {
    bool open = false;
    std::string parsedForJson;  // AppState::pendingAskBatchJson value we last parsed
    AskForm form;
    std::map<std::string, FieldState> fields;
};

DialogState& state() {
    static DialogState s;
    return s;
}

}  // namespace

bool drawAskDialog(app::AppState& s) {
    DialogState& d = state();

    if (s.pendingAskBatchJson.empty()) {
        d.open = false;
        return false;
    }

    // A new (or first) batch arrived: parse it and (re)seed widget state from `suggested`.
    if (s.pendingAskBatchJson != d.parsedForJson) {
        d.form = aicad::ui::parseAskBatch(s.pendingAskBatchJson);
        d.fields.clear();
        for (const auto& f : d.form.fields) initFieldState(d.fields[f.id], f);
        d.parsedForJson = s.pendingAskBatchJson;
        d.open = true;
        s.askDialogOpen = true;
        ImGui::OpenPopup("Agent question##askdialog");
    }

    bool submitted = false;
    ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(vp->Pos.x + vp->Size.x * 0.5f, vp->Pos.y + vp->Size.y * 0.5f),
                            ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    ImGui::SetNextWindowSize(ImVec2(420, 0), ImGuiCond_Appearing);

    bool stayOpen = true;
    if (ImGui::BeginPopupModal("Agent question##askdialog", &stayOpen,
                               ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings)) {
        for (const AskField& f : d.form.fields) {
            ImGui::PushID(f.id.c_str());
            ImGui::TextWrapped("%s", f.prompt.c_str());
            if (!f.why.empty()) {
                ImGui::SameLine();
                ImGui::TextDisabled("(?)");
                if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", f.why.c_str());
            }

            FieldState& fs = d.fields[f.id];
            switch (f.kind) {
            case AskFieldKind::Dimension: {
                const char* const* units; int n;
                unitsForFamily(f.family, units, n);
                ImGui::SetNextItemWidth(160);
                ImGui::InputDouble("##val", &fs.dimValue, 0.0, 0.0, "%.4g");
                ImGui::SameLine();
                ImGui::SetNextItemWidth(90);
                ImGui::Combo("##unit", &fs.dimUnitIdx, units, n);
                break;
            }
            case AskFieldKind::Choice: {
                std::vector<const char*> opts;
                opts.reserve(f.options.size());
                for (const auto& o : f.options) opts.push_back(o.c_str());
                if (!opts.empty()) {
                    ImGui::SetNextItemWidth(-1);
                    ImGui::Combo("##choice", &fs.choiceIdx, opts.data(), (int)opts.size());
                }
                break;
            }
            case AskFieldKind::Text:
                ImGui::SetNextItemWidth(-1);
                ImGui::InputText("##text", fs.textBuf.data(), fs.textBuf.size());
                break;
            case AskFieldKind::FileRequest:
                ImGui::SetNextItemWidth(-1);
                ImGui::InputTextWithHint("##path", "file path", fs.textBuf.data(), fs.textBuf.size());
                break;
            case AskFieldKind::Approval:
                ImGui::Checkbox("Approve", &fs.approved);
                break;
            }
            ImGui::Spacing();
            ImGui::PopID();
        }

        ImGui::Separator();
        if (ImGui::Button("Submit")) {
            std::map<std::string, std::string> raw;
            for (const AskField& f : d.form.fields) {
                FieldState& fs = d.fields[f.id];
                switch (f.kind) {
                case AskFieldKind::Dimension: {
                    const char* const* units; int n;
                    unitsForFamily(f.family, units, n);
                    char buf[96];
                    std::snprintf(buf, sizeof(buf), "%g %s", fs.dimValue,
                                  (fs.dimUnitIdx >= 0 && fs.dimUnitIdx < n) ? units[fs.dimUnitIdx] : "");
                    raw[f.id] = buf;
                    break;
                }
                case AskFieldKind::Choice:
                    raw[f.id] = (fs.choiceIdx >= 0 && fs.choiceIdx < (int)f.options.size())
                                    ? f.options[fs.choiceIdx] : std::string();
                    break;
                case AskFieldKind::Text:
                case AskFieldKind::FileRequest:
                    raw[f.id] = fs.textBuf.data();
                    break;
                case AskFieldKind::Approval:
                    raw[f.id] = fs.approved ? "true" : "false";
                    break;
                }
            }
            s.askAnswersJson = aicad::ui::buildAnswersJson(d.form, raw);
            s.pendingAskBatchJson.clear();
            s.askDialogOpen = false;
            d.open = false;
            d.parsedForJson.clear();
            submitted = true;
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel") || !stayOpen) {
            s.pendingAskBatchJson.clear();
            s.askDialogOpen = false;
            d.open = false;
            d.parsedForJson.clear();
            ImGui::CloseCurrentPopup();
        }

        ImGui::EndPopup();
    }

    return submitted;
}

}  // namespace ui
