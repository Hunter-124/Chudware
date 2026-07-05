// model_picker.cpp — shared model-picker widget.
// See model_picker.h for architecture notes.
#include "ui/model_picker.h"
#include "ui/model_registry.h"

#include "imgui.h"
#include "theme/theme.h"
#include "theme/domain_tokens.h"

#include <cstring>
#include <string>

namespace ui {

// ---------------------------------------------------------------------------
// drawSettingsSectionHeader
// ---------------------------------------------------------------------------

void drawSettingsSectionHeader(const char* label) {
    const float sc = theme::S();
    ImGui::Dummy(ImVec2(0, 6.f * sc));
    ImGui::SeparatorText(label);
}

// ---------------------------------------------------------------------------
// drawModelPicker
// ---------------------------------------------------------------------------

void drawModelPicker(bool offlineMode,
                     const ModelPickerSelection& sel,
                     const ModelPickerCallback& onPick,
                     bool closeOnPick)
{
    const float sc = theme::S();
    static char search[64] = {};
    if (ImGui::IsWindowAppearing()) {
        search[0] = 0;
        ImGui::SetKeyboardFocusHere();
    }
    ImGui::SetNextItemWidth(-1);
    ImGui::InputTextWithHint("##mpSearch", "Search models\xe2\x80\xa6", search, sizeof(search));
    ImGui::Separator();

    ImGui::BeginChild("##mpList", ImVec2(0, 240.f * sc), ImGuiChildFlags_None);

    bool picked = false;
    int  pickedProvider = 0;
    std::string pickedId, pickedBase;
    bool pickedVision = false;

    // Helper: draw one row.
    auto row = [&](int provider, const std::string& id,
                   const std::string& label, const std::string& baseUrl, bool vision)
    {
        // Filter by search string
        if (search[0]) {
            std::string q = search;
            for (char& c : q) c = (char)std::tolower((unsigned char)c);
            std::string hay = label + " " + id;
            for (char& c : hay) c = (char)std::tolower((unsigned char)c);
            if (hay.find(q) == std::string::npos) return;
        }
        bool active = (provider == sel.provider && id == sel.modelId);
        if (ImGui::Selectable(label.c_str(), active)) {
            picked = true;
            pickedProvider = provider;
            pickedId = id;
            pickedBase = baseUrl;
            pickedVision = vision;
        }
    };

    // 1. Built-in cloud models (Gemini / Anthropic / OpenAI)
    for (const auto& m : builtinModels())
        row(m.provider, m.id, m.label, "", m.vision);

    // 2. Cloud presets (hidden in offline mode)
    if (!offlineMode) {
        ImGui::Separator();
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
        ImGui::TextUnformatted("Cloud presets");
        ImGui::PopStyleColor();
        for (const auto& p : cloudPresets())
            row(PROV_CLOUDCOMPAT, p.id, p.label, p.baseUrl, p.vision);
    }

    // 3. Local presets (always shown)
    ImGui::Separator();
    ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
    ImGui::TextUnformatted("Local presets");
    ImGui::PopStyleColor();
    for (const auto& p : localPresets())
        row(PROV_LOCAL, p.id, p.label, p.baseUrl, p.vision);

    ImGui::EndChild();

    if (picked) {
        onPick(pickedProvider, pickedId, pickedBase, pickedVision);
        if (closeOnPick) ImGui::CloseCurrentPopup();
    }
}

}  // namespace ui
