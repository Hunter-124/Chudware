// core/piping/simhandoff.h — aicad.simhandoff/1 cross-sim handoff & verify
// envelope builders. C++ port of services/mcp/simhandoff.py (the executable
// form of contracts/sim_handoff.md): pure dict builders + a structural
// validator. The piping (piping→CFD) and hydraulic (hydraulic→piping,
// hydraulic→FEA) couplings both build envelopes through these.
#pragma once

#include <string>
#include <vector>

#include "piping/pyutil.h"

namespace aicad::piping::simhandoff {

using pyutil::ojson;

inline const char* SCHEMA = "aicad.simhandoff/1";

ojson study_ref(const std::string& domain, const std::string& tool,
                const ojson& doc_id = nullptr, const ojson& study = nullptr,
                const ojson& handoff_id = nullptr, const ojson& result_digest = nullptr);

ojson geometry_ref(const std::string& role, const std::string& kind,
                   const ojson& path = nullptr, const ojson& doc_id = nullptr,
                   const ojson& inline_doc = nullptr, const ojson& derived_by = nullptr,
                   const std::string& units = "mm", const ojson& meta = nullptr);

ojson condition(const std::string& name, const std::string& kind, const ojson& value,
                const std::string& units, const std::string& provenance,
                const ojson& applied_to = nullptr, const ojson& case_ = nullptr);

ojson expectation(const std::string& quantity, const ojson& value,
                  const std::string& units, const std::string& provenance);

ojson build_handoff(const std::string& coupling, const ojson& source, const ojson& target,
                    const ojson& geometry = nullptr, const ojson& conditions = nullptr,
                    const ojson& expectations = nullptr, const ojson& warnings = nullptr,
                    const ojson& meta = nullptr);

ojson check(const std::string& quantity, const std::string& units, const ojson& source_value,
            const ojson& target_value, const ojson& deviation_pct,
            const ojson& tolerance_pct, bool passed);

// Overall "pass" is COMPUTED as the AND of the individual check passes (never
// caller-supplied).
ojson build_verify(const std::string& coupling, const ojson& source, const ojson& target,
                   const ojson& checks, const std::string& verdict,
                   const ojson& handoff_id = nullptr, const ojson& advisories = nullptr,
                   const ojson& warnings = nullptr);

// Structural validation — JSON-path-prefixed error strings (empty = valid).
std::vector<std::string> validate_envelope(const ojson& env);

// Validate env and embed it under result["simhandoff"]. Throws
// std::runtime_error on an invalid envelope (a producer bug, like the
// oracle's ValueError). Returns result for chaining.
ojson attach(ojson result, const ojson& env);

}  // namespace aicad::piping::simhandoff
