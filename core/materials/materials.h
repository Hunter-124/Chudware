// core/materials/materials.h — the unified, provenance-stamped material registry
// (C++ port of engine_oracle/materials/registry.py). One
// first-party fact table (registry.json) read by the FEA seam, the DfM density
// defaults, the CAM CuttingDB fold-in and the material_lookup research helper.
// PURE: no OCCT, no doc/agent linkage; reads the SAME registry.json the Python
// oracle reads (env CHUDWARE_MATERIALS_REGISTRY override, then the appdata data root —
// the exact resolution core/cam/feeds.cpp uses).
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace chudware {
namespace materials {

using nlohmann::json;

// The 10 feeds/speeds fields the CuttingDB consumes (order matches core/cam/feeds.h).
extern const char* const kCamFields[10];

// Absolute path to the registry.json this process resolves ("" when undiscoverable).
std::string registry_path();
// True when the registry was found and parsed (all lookups fail closed otherwise).
bool available();

// Every material record keyed by canonical id (a copy; mutate freely). {} when absent.
json all_materials();
// Canonical registry key for a name/alias (case-insensitive), or "" if unknown.
std::string resolve_key(const std::string& name);
// Full record (mechanical + provenance + "key") for a name/alias, or null json.
json get(const std::string& name);
// density_g_cm3 for a name/alias, or null json when unknown.
json density(const std::string& name);

// The FEA mechanical library in the legacy _MATERIALS shape:
// {friendly_name: {"name","E_mpa","nu","density_g_cm3","yield_mpa","ultimate_mpa",
// "endurance_mpa"}}.
json fea_table();
// The legacy _MATERIAL_ALIASES map (alias -> friendly FEA key).
json fea_aliases();

// The "cam" sub-section for a material name/alias, or null json.
json cam_data(const std::string& name);
// The CuttingDB group id a registry material maps to, or "" when no cam correspondence.
std::string cam_material_id(const std::string& name);
// All registry materials carrying cam data: {canonical_id: cam_dict}.
json cam_materials();

// ---- single dispatch entry ------------------------------------------------------
// Owns "material_get", "material_density", "material_resolve_key", "material_fea_table",
// "material_fea_aliases", "material_cam_data", "material_cam_id", "material_cam_materials",
// "material_all". Returns false when the method is not ours. Result envelopes are
// {"ok":true,"result":<value>} with the Python value (null json for Python's None).
bool call(const std::string& method, const json& params, json& result, std::string& err);

}  // namespace materials
}  // namespace chudware
