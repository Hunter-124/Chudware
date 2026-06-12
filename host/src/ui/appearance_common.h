// Shared appearance / material picker (W1-B item 9).
//
// The RealView material preset table + the SolidWorks-style appearance color-picker popup used to
// be duplicated verbatim in feature_tree.cpp and viewport_panel.cpp. Both now call into here.
//
// appearanceColorPicker(): renders an ImGui popup (the CALLER opens it via ImGui::OpenPopup(popupId)
// after stashing which body the edit targets). Returns true exactly once — on commit — writing the
// chosen color into outColor[3] and, when a preset was picked, the material into
// outMetallic/outRoughness (a custom color leaves the material untouched so a previously-set metal
// keeps its finish).
#pragma once

namespace ui {

// C32-A RealView material preset (metallic, roughness, [r,g,b] tint).
struct MaterialPreset {
    const char* name;
    float metallic, roughness, rgb[3];
};

// The five presets shared by the feature tree and viewport appearance editors. Plastic restores the
// dielectric default (the C30 look).
extern const MaterialPreset kMaterials[5];

// The appearance editor popup. Returns true once on commit. See header note.
bool appearanceColorPicker(const char* popupId, float outColor[3],
                           float* outMetallic, float* outRoughness);

}  // namespace ui
