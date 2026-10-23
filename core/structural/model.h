// Structural document model — C++ port of the data half of
// engine_oracle/structural/: elements.py (aicad.structure/1 schema), plus the
// material / section-property / load-combo helpers from analysis.py and
// load_cases.py that the solver and the D/C checks consume.
//
// Units: N · mm · MPa throughout (forces N, distributed loads N/mm, lengths
// mm, E/G in N/mm^2, I in mm^4, A in mm^2).
#pragma once

#include <array>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>
#include <utility>

namespace chudware::structural {

// ---------------------------------------------------------------- elements

struct StructuralElement {
    std::string element_type;  // beam | column | post | joist | stud_wall | footing
    std::string id;
    std::string section_name;
    std::string material;
    double length_mm = 0.0;
    std::array<double, 16> transform;  // row-major rigid 4x4 placement
    // Local member DOFs [ux, uy, uz, rx, ry, rz] at i then j. A true entry
    // releases that member-end force from the connected node.
    std::array<bool, 12> end_releases{};
};

struct StructuralSupport {
    std::string element_id;
    bool at_j = false;
    // Restrained GLOBAL DOFs [ux, uy, uz, rx, ry, rz].
    std::array<bool, 6> restrained{};
};

struct StructureDoc {
    std::string name = "Structure1";
    std::string units = "mm";
    std::vector<StructuralElement> elements;
    std::vector<StructuralSupport> supports;
    std::vector<nlohmann::json> load_cases;  // contract §3 load-case dicts
};

extern const char* kSchema;  // "aicad.structure/1"

bool is_structure(const nlohmann::json& doc);

// Parse and validate an aicad.structure/1 dict, including canonical units,
// finite rigid transforms, unique IDs, optional supports/releases, and load
// container shape. Throws std::invalid_argument for invalid setup.
StructureDoc structure_doc_from_dict(const nlohmann::json& d);

// Global coordinates of the element's local (0,0,0) and (0,0,length_mm)
// through its row-major 4x4 transform (mirrors analysis.element_endpoints).
std::pair<std::array<double, 3>, std::array<double, 3>> element_endpoints(
    const StructuralElement& el);

// ---------------------------------------------------------------- materials

struct MaterialProps {
    double E, G, nu;  // MPa
};

// (E, G, nu) for a supported material family. Unknown keys are rejected
// rather than assigned an arbitrary softwood stiffness.
MaterialProps material_properties(const std::string& material);

// ---------------------------------------------------------------- sections

struct SectionProps {
    double E, G, nu, A, Iy, Iz, J;
    double S_major, S_minor;  // elastic section moduli paired with Iz / Iy
};

// Net section properties for AISC/lumber catalog shapes and validated solid,
// hollow rectangular, circular hollow, and I-section grammars. Unknown or
// physically impossible sections are rejected.
SectionProps section_properties(const std::string& section_name, const std::string& material,
                                const std::string& element_type = "");

// ---------------------------------------------------------------- load combos

struct LoadCombo {
    std::string name;
    double dead = 0.0, live = 0.0, wind = 0.0, snow = 0.0;

    // kind -> factor, only the nonzero kinds, in dead/live/wind/snow order
    // (mirrors LoadCombo.factors() dict insertion order).
    std::vector<std::pair<std::string, double>> factors() const;
};

// Preset ASCE 7 combos by name (case-insensitive). Throws
// std::out_of_range on an unknown name (mirrors load_cases.get_combo).
LoadCombo get_combo(const std::string& name);

}  // namespace chudware::structural
