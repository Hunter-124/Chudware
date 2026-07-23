// core/cam/audit.h — deterministic modal-subset G-code verifier (aicad.cam_audit/1).
// No LLM: parse G0/G1/G2/G3/G4 + modal words, then run envelope / feeds / tool /
// round-trip / rapid-below-clearance checks against the job + machine profile.
#pragma once

#include <string>
#include <vector>

#include "cam/model.h"

namespace chudware {
namespace cam {

// Audit posted (or supplied) G-code. `source` is "cam_post" or "supplied".
// Returns the aicad.cam_audit/1 envelope.
json audit_program(const Job& job,
                   const std::string& gcode,
                   const std::string& dialect,
                   const Machine& profile,
                   const std::vector<Toolpath>& toolpaths,
                   const std::string& source,
                   const std::string& machine_profile_id = {});

}  // namespace cam
}  // namespace chudware
