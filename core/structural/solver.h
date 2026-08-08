// Validated direct-stiffness 3D frame solver.
//
//   * implicit legacy footing/lowest-node fixity or explicit per-DOF supports
//   * statically condensed member-end releases
//   * point and distributed loads in normalized GLOBAL directions
//   * linear-static ASCE 7 load combinations with rank-based instability checks
//   * member force/deflection axis diagnostics and support reactions
//
// Beam fixed-end and segment equations follow PyNite's Member3D, BeamSegY/Z,
// and FixedEndReactions formulations. Canonical units are N · mm · MPa.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "structural/model.h"

namespace chudware::structural {

struct AnalysisRow {
    std::string element_id;
    std::string combo;
    double axial_n = 0.0;        // +tension
    double shear_n = 0.0;        // governing signed local-y/local-z shear
    double moment_nmm = 0.0;     // governing signed local-y/local-z moment
    double deflection_mm = 0.0;  // governing signed local-y/local-z deflection
    // Additive diagnostics retain the two section-axis components needed for
    // biaxial stress checks without changing the legacy governing channels.
    double shear_y_n = 0.0;
    double shear_z_n = 0.0;
    double moment_y_nmm = 0.0;
    double moment_z_nmm = 0.0;
    double deflection_y_mm = 0.0;
    double deflection_z_mm = 0.0;

    nlohmann::json to_dict() const;
};

struct ReactionRow {
    std::string combo;
    double x_mm = 0.0, y_mm = 0.0, z_mm = 0.0;
    double fx_n = 0.0, fy_n = 0.0, fz_n = 0.0;
    double mx_nmm = 0.0, my_nmm = 0.0, mz_nmm = 0.0;

    nlohmann::json to_dict() const;
};

// End-to-end analysis (mirrors analysis.analyze_doc): build the frame model
// from the doc, apply its contract load cases, solve each combo, and return
// one row per member per combo (member order = doc element order).
// Throws std::invalid_argument (schema/model errors) / std::runtime_error
// (singular stiffness).
std::vector<AnalysisRow> analyze_doc(
    const StructureDoc& doc, const std::vector<LoadCombo>& combos,
    std::vector<ReactionRow>* reactions = nullptr);

}  // namespace chudware::structural
