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

// Stateless dry-run validation (engine.doc.ops dry_run, used by the agent modeler/
// fixer path). Clones the live session's current doc into a scratch session and runs
// the op batch against it WITHOUT mutating the live session or bumping its revision.
// Returns {"ok": true} on success, or {"ok": false, "error": "<message>"} where the
// error string is composed EXACTLY like ops_emit.dry_run: the OpError message, and if
// detail.all_errors is present, message + " | " + "; ".join(all_errors[:5]).
nlohmann::json doc_dry_run_ops(const std::string& doc_id, const nlohmann::json& ops);

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

// Instant3D push-pull: drag face (1-based body_id, 1-based topo_id) by `distance` mm
// along its outward normal. Plans the drivable feature param (plan_push_pull), and on
// a real change applies it, rebuilds, and returns the plan fields + ops-response. A
// non-drivable face returns {draggable:false, reason}; a value that would collapse the
// body rolls back. `commit` (default false = transient preview) pushes one undo entry.
// Port of engine/doc/instant3d.py push_pull. Throws OpError for a bad body_id/topo_id.
nlohmann::json doc_push_pull(const std::string& doc_id, int body_id, int topo_id,
                             double distance, bool commit);

// C19 sim bridge — produce sim INPUTS from the live doc session (port of
// engine/sim/bridge.py via engine/worker.py _doc_export_step / _doc_face_points).
//
// doc_export_step: write the surviving solid bodies to `path` for the mesher. The
// format follows the extension (.stl -> binary STL, else STEP/AP214). Single body
// exports as-is; multiple bodies combine into a compound. Returns {"path": <abs>}.
// Throws OpError("export_failed", "document has no solid bodies to export") on empty.
nlohmann::json doc_export_step(const std::string& doc_id, const std::string& path);

// doc_face_points: resolve the face `selector` (kind=="face") against the LAST body's
// faces and sample that face's triangulation nodes in WORLD space, down-sampled to at
// most `max_n` points (uniform stride). Returns {"points": [[x,y,z],...], "tol_mm":
// max(0.5, diag*0.02)}. Throws OpError on a non-face selector / no bodies / a dangling
// face / no triangulation nodes (falling back to the selector centroid first).
nlohmann::json doc_face_points(const std::string& doc_id, const nlohmann::json& selector,
                               int max_n = 400);

// Evaluate — mass properties of the active body (port of engine/doc/evaluate.py::mass_props
// via engine/worker.py::_eval_mass). body_id is 1-based; <= 0 selects the LAST body. Returns
// {volume_mm3, mass_g, com[3], inertia[9] (row-major, kg·mm² scaled by density), principal_
// moments[3], principal_axes[3][3], material{density_g_cm3}}. A surface body returns the
// {is_surface, area_mm2, ...} report. Throws OpError on no bodies / a bad body_id.
nlohmann::json doc_eval_mass(const std::string& doc_id, int body_id, double density_g_cm3);

// Export the document's surviving solid bodies to `path` in `fmt` (step/iges/brep/stl)
// via OCCT. Returns {"path": <abs>}. Throws OpError("export_failed") on no bodies / an
// unsupported format / a writer failure (engine.doc.formats export_file).
nlohmann::json doc_export_file(const std::string& doc_id, const std::string& path,
                               const std::string& fmt);

// Import a neutral B-rep file (step/iges/brep) as a new `import` feature. With `into`
// (a doc id) it appends to that session; otherwise it creates a new part session.
// Returns the ops-response + "doc_id" (engine.doc.formats import_file).
nlohmann::json doc_import_file(const std::string& path, const std::string& into,
                               bool as_solid, const std::string& fmt);

// STATELESS diagnostic: read a mesh file into its RAW per-object meshes (no sewing) and
// return {"objects": [{"name", "verts":[x,y,z,...], "tris":[i,j,k,...]}, ...]}. Used by
// the 3MF bit-parity gate to compare the C++ lib3mf extraction against the Python oracle
// (engine.doc.formats._import_3mf). `fmt` defaults to detect_format(path) when empty.
nlohmann::json mesh_read_raw(const std::string& path, const std::string& fmt);

// Persist the session to `path` (or its current path if `path` is empty) as a
// .step+ container. Clears the dirty flag, records the path. Returns {"path": ...}.
// Throws OpError("io_error") if no path is available, or on a write failure.
nlohmann::json doc_save(const std::string& doc_id, const std::string& path);

// Open a .step+ container at `path` into a FRESH session, rebuild it, and return the
// ops-response + "doc_id" + "warnings". Throws OpError("io_error") on a bad file.
nlohmann::json doc_open(const std::string& path);

}  // namespace aicad::doc
