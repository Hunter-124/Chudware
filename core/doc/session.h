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
