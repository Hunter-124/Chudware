// core/parts/place.h — universal standard-part insertion router.
// C++ port of engine_oracle/parts_hub/place.py: routes a
// PartRecord.geometry_ref to the correct domain insertion path, landing an
// EDITABLE document-schema op in every case (R1 parity — never an opaque body).
//
// Routing table (keyed on geometry_ref["kind"], per contracts/parts.md §2):
//
//   core_family      → parts_make seam (libchudware wires doc_new + parts::build)
//   generate_model   → build_model seam (bolts match → STEP) → import_file leg
//   symbol_footprint → ecad sch_add_symbol op via chudware::ecad::apply_ops (in-core)
//   section_profile  → append StructuralElement dict to an aicad.structure/1 doc
//   fitting_id       → chudware::piping::new_fitting into an aicad.piping/1 doc
//   fmu_component    → chudware::hydraulic::new_component into an aicad.hydraulic/1 doc
//   import_file      → import_file seam (browser-acquired STEP/IGES)
//
// The PartRecord lookup (services/parts hub) and the two doc-creating RPCs are
// NOT core concerns, so — exactly like core/sourcing — they are Seams
// injections: libchudware.cpp supplies the production defaults and tests stub
// them. Every placer returns a PlacementResult dict mirroring the oracle
// byte-for-byte:
//
//   {ok, domain, doc_id, entity_id, editable, detail, reason, hint}
//
// Doc-mutating legs (symbol_footprint / section_profile / fitting_id /
// fmu_component) additionally carry the updated document under result["doc"]
// (the oracle mutates the caller's dict in place; across an RPC boundary the
// updated doc must be handed back explicitly — additive key, oracle results
// are unaffected). Error contract: always a result dict, never throws.
#pragma once

#include <functional>
#include <string>

#include <nlohmann/json.hpp>

namespace chudware::parts {

using nlohmann::json;

// Side-effect injection points (mirrors chudware::sourcing::Seams). An unset
// std::function makes the dependent leg fail with the oracle's "native core
// unavailable" error envelope instead of crashing.
struct PlaceSeams {
    // PartRecord lookup by part_id (services/parts hub). Returns null json
    // when the part_id is unknown (oracle: parts_hub.get -> None).
    std::function<json(const std::string& part_id)> get_record;
    // parts_make RPC: {family, size, length_mm?, ...} -> {ok, doc_id, name,
    // bbox, datum, error?}. libchudware wires the existing parts_make dispatch.
    std::function<json(const json& call_params)> parts_make;
    // import_file RPC: (path, as_solid) -> {doc_id, body_count, ...}.
    std::function<json(const std::string& path, bool as_solid)> import_file;
    // spec_sheet_matcher.generate_model: bolts `match` dict -> saved model
    // path ("" when not buildable). Same shape as sourcing::Seams::build_model.
    std::function<std::string(const json& match)> build_model;
};

// Dispatch one placement method. Returns false when `method` is not a ported
// placement method (caller falls through to the next dispatch layer / the
// Python worker). Returns true when handled: `result` holds the
// PlacementResult envelope (ok=false on expected failures — the oracle never
// raises either). `err` is reserved for dispatch-level failures and stays
// empty today.
//
// Ported methods (mirroring place.place + place._place_import_file):
//   part_place       {part_id, record?, doc?, params?}  → PlacementResult
//                    (`record` pre-fetched skips the get_record seam)
//   part_import_file {path, as_solid?}                  → PlacementResult
//                    (the import_artifact MCP tool's _place_import_file call)
bool place_call(const std::string& method, const json& params, json& result, std::string& err,
                const PlaceSeams& seams);

}  // namespace chudware::parts
