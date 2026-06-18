// ECAD document session — C++ port of engine/ecad/session.py (EcadSession +
// new/apply_ops/undo/redo/tree/close, revision counter, bounded JSON-snapshot
// undo/redo, stale-revision guard) over the ecad ops vocabulary.
//
// The ops-response shape mirrors the Python session: {revision, id_map, kind,
// tree, compile [, warnings]}; ecad_new adds "doc_id"; ecad_tree returns
// {revision, kind, tree, compile}. The `compile` field (netlist/ERC for sch,
// DRC/ratsnest for pcb) is DEFERRED to a later wave — this foundation returns
// the same skeleton the Python session produces when those sub-modules are
// absent (netlist:null, erc:[], drc:[], ratsnest:[], stats:{}).
#pragma once

#include <string>

#include <nlohmann/json.hpp>

#include "ecad/ops.h"  // OpError

namespace aicad::ecad {

// ecad_new {kind: "sch"|"pcb", name?} -> ops-response + "doc_id".
nlohmann::json ecad_new(const std::string& kind, const std::string& name);

// ecad_apply_ops. base_revision < 0 skips the optimistic-concurrency guard;
// a mismatch throws OpError("stale_revision").
nlohmann::json ecad_apply_ops(const std::string& doc_id, long base_revision,
                              const nlohmann::json& ops);

nlohmann::json ecad_undo(const std::string& doc_id);     // bumps revision +1
nlohmann::json ecad_redo(const std::string& doc_id);     // bumps revision +1
nlohmann::json ecad_tree(const std::string& doc_id);     // no rebuild, no rev bump
nlohmann::json ecad_rebuild(const std::string& doc_id);  // recompute, no rev bump
bool ecad_close(const std::string& doc_id);

// ecad_describe {doc_id} -> minimal summary (the fallback the Python rpc builds
// when engine.ecad.describe is absent).
nlohmann::json ecad_describe(const std::string& doc_id);

// ecad_schema {kind?} -> the empty document for the given kind (cheap diagnostic).
nlohmann::json ecad_schema(const std::string& kind);

}  // namespace aicad::ecad
