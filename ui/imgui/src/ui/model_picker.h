// model_picker.h — shared model-picker widget (search + builtin/cloud/local presets).
//
// Callback-driven so it works for:
//   (A) settings AI tab / vision picker  → mutates AppSettings via callback
//   (B) composer dropdown in agent_panel → same callback pattern
//   (C) T-W17 agent_settings_panel       → caller supplies its own callback operating on
//       SettingsSource; only the preset *list* + UI chrome are reused here.
//
// Single source of truth for preset lists: model_registry.h (builtinModels / localPresets
// / cloudPresets).  Do NOT duplicate those lists in callers.
#pragma once

#include <functional>
#include <string>

namespace ui {

// Describes the currently selected model (for display and default-selection logic).
struct ModelPickerSelection {
    int         provider = 4;   // PROV_* index
    std::string modelId;        // may be empty → shows "(default)"
};

// Callback invoked when the user picks a model.
// provider: PROV_* constant; modelId / baseUrl: from the preset or typed manually.
// vision: true if the selected preset is vision-capable (caller may update s.vision).
using ModelPickerCallback = std::function<void(int provider,
                                               const std::string& modelId,
                                               const std::string& baseUrl,
                                               bool vision)>;

// drawModelPicker — renders the search + grouped list popup body.
// Call inside ImGui::BeginPopup/EndPopup or directly in a child region.
// offlineMode: when true, cloud presets are hidden.
// sel: current selection (used to highlight the active row).
// onPick: called immediately when the user selects a row; caller is responsible for
//         calling ImGui::CloseCurrentPopup() if this is inside a popup — pass
//         closeOnPick=true to have the picker do it automatically.
void drawModelPicker(bool offlineMode,
                     const ModelPickerSelection& sel,
                     const ModelPickerCallback& onPick,
                     bool closeOnPick = true);

// drawProviderModelForm — renders the full provider+model+URL+key form used in the
// AI Models & Routing settings tab.  Mutates the three output parameters directly
// (caller owns AppSettings and calls saveAppSettings when needed).
// Returns true if any field changed.
struct ProviderFormState {
    int   provider   = 4;
    char* model      = nullptr;   // char[512]
    char* apiKey     = nullptr;   // char[256]  (legacy / local)
    char* geminiKey  = nullptr;
    char* anthropicKey = nullptr;
    char* openaiKey  = nullptr;
    char* cloudKey   = nullptr;
    char* baseUrl    = nullptr;   // char[256]
    bool* vision     = nullptr;
    bool* offlineMode = nullptr;
};

// drawSettingsSectionHeader — draws a styled SeparatorText with consistent vertical
// spacing.  Use in place of raw ImGui::SeparatorText + Dummy calls.
void drawSettingsSectionHeader(const char* label);

}  // namespace ui
