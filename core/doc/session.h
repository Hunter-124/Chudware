// Stateful document session — C++ port of engine/doc/session.py (DocSession +
// the apply_ops/undo/redo machinery) over the op vocabulary of engine/doc/ops.py,
// on top of compile_document.
//
// This is the PRODUCTION-CUTOVER layer: it lets the host's stateful document RPCs
// (doc_new / doc_apply_ops / doc_undo / doc_redo / doc_tree / doc_rebuild) run
// in-process in the C++ core instead of the Python worker. Each call returns the
// ops-response JSON the host already consumes:
//   { revision, id_map, tree, rebuild:{feature_status,doc_status}, tess:{aicm_path,
//     bodies}, owner_map }
// (doc_new adds "doc_id"; doc_tree returns only revision/tree/owner_map).
//
// SCOPE (first increment): blank PART docs + the core part ops (add/edit/delete/
// reorder feature, suppress/rename, units/material/rollback, equations) + the core
// sketch ops (add/edit/delete entity, add/delete constraint, set_dim, toggle
// construction). Advanced sketch tools (trim/fillet/offset/mirror/pattern),
// assemblies, configurations, copy/paste, derived features, full document
// validation, and the owner_map topology are DEFERRED (owner_map returns {}).
#pragma once

#include <stdexcept>
#include <string>

#include <nlohmann/json.hpp>

namespace aicad::doc {

// A contract violation (mirrors engine.doc.ops.OpError) -> the host's structured
// error envelope {code, message, detail}. Thrown across doc_* and converted at the
// libaicad boundary into a nonzero return carrying that envelope.
struct OpError : std::runtime_error {
    std::string code;
    nlohmann::json detail;
    OpError(std::string c, const std::string& msg, nlohmann::json d = nlohmann::json::object())
        : std::runtime_error(msg), code(std::move(c)), detail(std::move(d)) {}
    nlohmann::json envelope() const {
        return nlohmann::json{{"code", code}, {"message", what()}, {"detail", detail}};
    }
};

// Create a new blank document ("part"; "assembly" not yet supported). Returns the
// ops-response + "doc_id". Registers the session in the process-global store.
nlohmann::json doc_new(const std::string& kind, const std::string& name);

// Apply an op batch. `base_revision` < 0 skips the optimistic-concurrency guard;
// otherwise a mismatch throws OpError("stale_revision"). A bad op throws OpError.
// `coalesce` folds the batch into the previous undo step (no new snapshot).
nlohmann::json doc_apply_ops(const std::string& doc_id, long base_revision,
                             const nlohmann::json& ops, bool coalesce);

nlohmann::json doc_undo(const std::string& doc_id);     // bumps revision +1
nlohmann::json doc_redo(const std::string& doc_id);     // bumps revision +1
nlohmann::json doc_tree(const std::string& doc_id);     // no rebuild, no rev bump
nlohmann::json doc_rebuild(const std::string& doc_id);  // forced rebuild, no rev bump
bool doc_close(const std::string& doc_id);

// Enumerate the open C++ sessions: {"docs":[{doc_id,name,kind,path,revision,dirty}]}.
// Mirrors engine.doc.session.list_sessions (metadata only — no compile).
nlohmann::json doc_list();

// PropertyManager-cancel checkpoints (engine.doc.session.checkpoint/revert).
// doc_checkpoint deep-snapshots the current doc and returns {"checkpoint": id}.
// doc_revert restores the snapshot, pushes ONE undo entry of the pre-revert doc,
// rebuilds, bumps the revision, and returns the ops-response. Checkpoints are
// per-session and cleared on close. A bad id throws OpError("invalid_op").
nlohmann::json doc_checkpoint(const std::string& doc_id);
nlohmann::json doc_revert(const std::string& doc_id, const std::string& checkpoint_id);

// Named configurations (engine.doc.session config_*). config_list is read-only
// ({configurations, active, detail}); the mutators edit the doc's configurations
// list, validate the result, push one undo step, bump the revision, recompile the
// active config, and return the ops-response. A bad name / duplicate / last-config
// delete / invalid override throws OpError("invalid_op").
nlohmann::json doc_config_list(const std::string& doc_id);
nlohmann::json doc_config_add(const std::string& doc_id, const std::string& name,
                              const nlohmann::json& copy_from);
nlohmann::json doc_config_delete(const std::string& doc_id, const std::string& name);
nlohmann::json doc_config_activate(const std::string& doc_id, const std::string& name);
nlohmann::json doc_config_set_override(const std::string& doc_id, const std::string& name,
                                       const std::string& feature_id, const std::string& param,
                                       const nlohmann::json& value, bool clear);
nlohmann::json doc_config_set_suppression(const std::string& doc_id, const std::string& name,
                                          const std::string& feature_id, bool suppressed);

// Resolve a viewport pick (1-based body ordinal + kind + 1-based MapShapes topo id)
// into a selector json {selector: {...}}. Throws OpError("unknown_doc"/...).
nlohmann::json doc_resolve_pick(const std::string& doc_id, int body_id,
                                const std::string& kind, int topo_id);

// Persist the session to `path` (or its current path if `path` is empty) as a
// .step+ container. Clears the dirty flag, records the path. Returns {"path": ...}.
// Throws OpError("io_error") if no path is available, or on a write failure.
nlohmann::json doc_save(const std::string& doc_id, const std::string& path);

// Open a .step+ container at `path` into a FRESH session, rebuild it, and return the
// ops-response + "doc_id" + "warnings". Throws OpError("io_error") on a bad file.
nlohmann::json doc_open(const std::string& path);

}  // namespace aicad::doc
