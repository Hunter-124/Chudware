// Structural RPC dispatch — the single entry core/api/libchudware.cpp
// dispatches to. Mirrors the frozen surface of
// contracts/structural_schema.md §3 as implemented by
// engine_oracle/structural/{analysis,checks,estimate}.py behind
// services/mcp/structural_tools.py: dict in → dict out, expected failures come
// back INSIDE the result as the {code, message, detail} error envelope.
//
// Methods:
//   struct_analyze  {doc|doc_path, load_combo}  → {load_combo, units, results, reactions}
//   struct_check    {doc|doc_path, load_combo}  → {load_combo, complete, disclaimer, results}
//   struct_estimate {doc|doc_path, rates_path?} → {complete, line_items, fastener_line, totals…}
#pragma once

#include <string>

#include <nlohmann/json.hpp>

namespace chudware::structural {

// Dispatch one struct_* method. Returns false when `method` is not a ported
// structural method (caller falls through to the next dispatch layer / the
// Python worker). Returns true when handled: `result` holds either the
// method's result or the {code, message, detail} error envelope. `err` is
// reserved for dispatch-level failures and stays empty today.
bool call(const std::string& method, const nlohmann::json& params, nlohmann::json& result,
          std::string& err);

}  // namespace chudware::structural
