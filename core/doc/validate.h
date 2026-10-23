// Structural JSON validator for aicad.part/1 documents — a faithful C++ port of
// engine/doc/schema.py's PART-document validation path (validate_part and all the
// helpers it reaches: _validate_common / _validate_appearances / _validate_equations /
// _validate_rollback / _validate_configurations / validate_feature / validate_entity /
// validate_constraint / validate_dim / validate_selector / _validate_sel_or_datum /
// _handle_targets_entity / validate_eq_curve_expr).
//
// This is the executable form of contracts/doc_schema.md + feature_params.md:
// STRUCTURAL validation only (envelopes, types, arities, handles, selectors, dimension
// values, expression grammar + name resolution). Geometric/semantic validation
// (does the profile close, does the axis cross the profile) belongs to the compiler,
// not here.
//
// Accept/reject parity with the Python oracle is the load-bearing contract: this
// returns an empty vector iff Python's validate_document returns empty. Each entry is
// a JSON-path-ish locator-prefixed error string so failures are actionable.
//
// Only aicad.part/1 is supported here (the C++ session is parts-only). Any other
// schema returns a single-element {"schema: unknown '<schema>'"}.
//
// Self-contained: the document-equation grammar (validate_expr / expr_names) and the
// eq-curve grammar are reproduced inside validate.cpp; this does NOT depend on
// core/util/expr.{h,cpp} (which is the EVALUATING path — this validator never
// evaluates: "1/0" is grammatically valid here, and an unknown NAME is allowed at
// grammar time, resolved later).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace chudware::doc {

// Validate a document. Returns the list of structural error strings (empty == valid).
// Dispatches on doc["schema"]; only "aicad.part/1" is supported.
std::vector<std::string> validate_document(const nlohmann::json& doc);

// Keys of FEATURE_TYPES (add_feature.feature.type vocabulary). Sorted.
// Owned here so MCP / op_registry / tests never hand-copy the list.
std::vector<std::string> feature_type_names();

// OP-PATH ONLY (add_feature / edit_feature). One error per key in `params` that the
// compiler does not read for a feature of `type`; each error names the key, a hint for
// a known invented key ("reverse" -> "flip"), and the accepted params. Null values are
// skipped (edit_feature deletes a key with null). Types without a known-key table
// return {}. Never call it from validate_document: saved documents can carry legacy
// inert keys, and a whole-document check would block every later edit to them.
std::vector<std::string> unknown_feature_params(const std::string& type,
                                                const nlohmann::json& params);

// Rewrite a feature's SHAPE (not its intent) into what validate_document accepts:
// wrap a single value handed to a list-form ref slot ("profile":"$0" -> ["$0"]).
// Returns one human-readable note per rewrite, so the caller can echo the
// correction; an empty result means the feature was already canonical. Driven by
// the same FEATURE_TYPES table the validator uses, so it cannot drift from the
// rules it is satisfying. Param DEFAULTS are deliberately not done here — see the
// scope note on the definition in validate.cpp.
std::vector<std::string> normalize_feature_shape(nlohmann::json& feature);

}  // namespace chudware::doc
