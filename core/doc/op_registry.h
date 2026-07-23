// Closed document-op vocabulary — SINGLE SOURCE for apply_one dispatch names.
//
// When you add a handler in session.cpp::apply_one, you MUST also add the op
// name to kDocOpKinds in op_registry.cpp. test_op_registry fails if the lists
// diverge (every registry name must be accepted by dispatch; unknown-op errors
// must cite this list for suggestions).
//
// Feature-type names (add_feature.feature.type) live in validate.cpp FEATURE_TYPES
// and are re-exported here via feature_type_names() so MCP/agent surfaces can
// document both the op envelope and the type vocabulary from one query.
#pragma once

#include <cstddef>
#include <string>
#include <vector>

namespace chudware::doc {

// Canonical list of document op kinds accepted by apply_one.
std::vector<std::string> doc_op_kinds();

// Feature types accepted under op "add_feature" (from FEATURE_TYPES).
std::vector<std::string> feature_type_names();

// Closest N op kinds for an unknown name. Prefers prefix / token-family
// (sketch_*, asm_*) matches over pure Levenshtein; also boosts add_feature when
// the unknown matches a feature type (models often emit type names as ops).
std::vector<std::string> suggest_doc_ops(const std::string& unknown, std::size_t n = 5);

// Build the standard rejection message used by apply_one:
//   op[<idx>]: unknown op '<k>'. closest: a, b, c; see cad_guide()
// When `k` matches a feature type, also notes the add_feature envelope.
std::string unknown_doc_op_message(std::size_t idx, const std::string& k);

// Compact cheat-sheet block for agent tool-result injection (one-shot per mission).
std::string doc_ops_cheatsheet_snippet();

// JSON payload for the doc_list_ops RPC / Python bridge:
//   {ops:[...], feature_types:[...], example_add_feature:{...}}
// Implemented in op_registry.cpp as nlohmann via free function in session/libchudware.
// Declared here as a string-building helper that does not pull json headers into
// every TU; the JSON form is assembled at the RPC boundary.

}  // namespace chudware::doc
