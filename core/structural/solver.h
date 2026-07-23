// Direct-stiffness 3D frame solver (T-RF19 leaf 5) — a from-scratch C++
// reimplementation of the PyNite 3.0.0 subset that
// engine_oracle/structural/analysis.py exercises:
//
//   * nodes / members / fully-fixed supports (footings, else the lowest node)
//   * member point + trapezoidal distributed loads in GLOBAL directions
//     (FX/FY/FZ — the only directions apply_load_cases ever emits)
//   * linear-static first-order analysis per ASCE 7 load combination
//   * member end forces f = ke·d + fer, then PyNite's beam-segmentation
//     post-processing (_segment_member / BeamSegY / BeamSegZ) for
//     max/min shear, moment, and 100-point-sampled deflection
//
// The formulas are ported line-for-line from PyNite's Member3D.py /
// BeamSegY.py / BeamSegZ.py / FixedEndReactions.py so member forces and
// deflections match the Python oracle to linear-statics precision. Units:
// N · mm · MPa.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "structural/model.h"

namespace aicad::structural {

struct AnalysisRow {
    std::string element_id;
    std::string combo;
    double axial_n = 0.0;      // +tension (contract sign — PyNite axial negated)
    double shear_n = 0.0;      // max |shear| over Fy/Fz, sign preserved
    double moment_nmm = 0.0;   // max |moment| over Mz/My, sign preserved
    double deflection_mm = 0.0;  // max |deflection| over dy/dz, sign preserved

    // Mirrors AnalysisResult.to_dict() — dc_ratio / pass stay null (struct_check fills them).
    nlohmann::json to_dict() const;
};

// End-to-end analysis (mirrors analysis.analyze_doc): build the frame model
// from the doc, apply its contract load cases, solve each combo, and return
// one row per member per combo (member order = doc element order).
// Throws std::invalid_argument (schema/model errors) / std::runtime_error
// (singular stiffness).
std::vector<AnalysisRow> analyze_doc(const StructureDoc& doc,
                                     const std::vector<LoadCombo>& combos);

}  // namespace aicad::structural
