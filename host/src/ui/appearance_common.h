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
//
// The popup also exposes Metalness and Roughness sliders (shown when outMetallic && outRoughness are
// non-null): they edit the live PBR finish for ANY color — including a custom RGB — so Apply captures
// the chosen metalness/roughness without first clicking a named material. Sixteen quick color presets
// (two rows of eight) sit above the full color wheel, and each material preset button shows its
// characteristic color as a swatch beside the name.
#pragma once

#include <string>

namespace ui {

// C32-A RealView material preset (metallic, roughness, [r,g,b] tint).
struct MaterialPreset {
    const char* name;
    float metallic, roughness, rgb[3];
};

// Number of shared RealView material presets in kMaterials.
constexpr int kMaterialsCount = 5;

// The shared presets used by the feature tree and viewport appearance editors. Plastic restores the
// dielectric default (the C30 look).
extern const MaterialPreset kMaterials[kMaterialsCount];

// W2 (material UX): physical materials surfaced by the FeatureManager "Material" node's right-click
// menu. Each pairs an engine sim material key (engine/sim/materials.json — what an FEA study reads)
// with the display appearance the part takes when that material is assigned. Choosing one both feeds
// the simulation (the doc's material key, via set_material) and recolours the part (a derived
// set_appearance); an Appearance edit applied afterwards still wins, being a later set_appearance.
struct PhysicalMaterial {
    const char* key;    // engine/sim/materials.json key (the FEA study material)
    const char* name;   // menu label
    float metallic, roughness, rgb[3];   // derived display appearance
};

// Curated common engineering materials offered by the Material node's menu.
constexpr int kPhysicalMaterialsCount = 9;
extern const PhysicalMaterial kPhysicalMaterials[kPhysicalMaterialsCount];

// Material -> appearance map: fills outColor/outMetallic/outRoughness with the display look for an
// engine material key. A curated key (kPhysicalMaterials) maps exactly; any other key (a custom or
// less-common engine material) is categorised by name onto a kMaterials preset so it still gets a
// sensible finish. Always resolves a look (Plastic is the dielectric fall-through) and returns true.
bool materialAppearance(const std::string& key, float outColor[3],
                        float& outMetallic, float& outRoughness);

// Density (g/cm³) for an engine material key. Looks up engine/sim/materials.json (lazy), then
// kPhysicalMaterials heuristics, then category fallbacks. Unknown/empty key → 7.85 (steel).
float materialDensityGcm3(const std::string& key);

// Young's modulus (MPa) for axial stiffness estimates. 0 when unknown.
float materialYoungsEMpa(const std::string& key);

// Friendly display name for a material key (kPhysicalMaterials name, else the key, else "(not set)").
std::string materialDisplayName(const std::string& key);


// The appearance editor popup. Returns true once on commit. A nonempty disabledReason disables
// Apply and immediate-apply presets without discarding the working color; Cancel remains available.
bool appearanceColorPicker(const char* popupId, float outColor[3],
                           float* outMetallic, float* outRoughness,
                           const char* disabledReason = nullptr);

}  // namespace ui
