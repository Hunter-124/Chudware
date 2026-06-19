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

#include <array>
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

// Weldment cut list — group every weld_member structural member into a SolidWorks
// "Weldment Cut List" (port of engine/doc/cutlist.py::cut_list_for_session). Forces a
// FRESH compile so the weld_member compiler repopulates the CompileResult.weld_members
// side-channel, then groups pooled members by (profile, resolved dims, length@0.1mm).
// Returns {rows:[{item, profile, description, length_mm, quantity, total_length_mm}],
// total_members, total_length_mm, profiles:[...]}. Rows sort by profile asc then length
// desc; item is 1-based post-sort. An assembly / a part with no weld_member feature
// yields empty rows + zero totals (NOT an error).
nlohmann::json doc_weld_cut_list(const std::string& doc_id);

// Evaluate — mass properties of the active body (port of engine/doc/evaluate.py::mass_props
// via engine/worker.py::_eval_mass). body_id is 1-based; <= 0 selects the LAST body. Returns
// {volume_mm3, mass_g, com[3], inertia[9] (row-major, kg·mm² scaled by density), principal_
// moments[3], principal_axes[3][3], material{density_g_cm3}}. A surface body returns the
// {is_surface, area_mm2, ...} report. Throws OpError on no bodies / a bad body_id.
nlohmann::json doc_eval_mass(const std::string& doc_id, int body_id, double density_g_cm3);

// Evaluate — geometry-validity report of the active body (port of evaluate.py::check via
// worker.py::_eval_check). Runs BRepCheck_Analyzer + free-edge / tiny-edge / sliver-face
// census. Returns {"issues":[{kind,severity,text}]} (empty issues == valid). body_id is
// 1-based; <= 0 selects the LAST body. Throws OpError on no bodies / a bad body_id.
nlohmann::json doc_eval_check(const std::string& doc_id, int body_id);

// Evaluate — cross-section properties of the LAST body at `plane` (port of
// evaluate.py::section_props via worker.py::_eval_section_props). `plane` is a reserved
// planar datum id ("plane.top/front/right") or a {"kind":"datum","feature":<id>} selector.
// Returns {area_mm2, centroid[3], Ixx, Iyy, Ixy}. Matches the oracle's degenerate path
// (area/moments == 0; only the section-wire centroid is meaningful). Throws OpError on no
// bodies / a non-reserved-planar plane.
nlohmann::json doc_eval_section_props(const std::string& doc_id, const nlohmann::json& plane_ref);

// Evaluate — mirror-symmetry of the active body about `plane` (port of evaluate.py::
// symmetry via worker.py::_eval_symmetry). Returns {symmetric, deviation_mm3} where
// deviation is the symmetric-difference volume. body_id 1-based, <= 0 = LAST. `plane` as
// for section_props. Throws OpError on no bodies / a bad body_id / a bad plane.
nlohmann::json doc_eval_symmetry(const std::string& doc_id, int body_id,
                                 const nlohmann::json& plane_ref);

// Evaluate — pairwise interference + clearance across ALL surviving bodies (port of
// evaluate.py::interference via worker.py::_eval_interference, part-doc branch). Returns
// {ok, summary, parts:[{name,volume,bbox}], interferences:[{a,b,volume}], clearances:
// [{a,b,distance}]}. Body names are "b1".."bN" in AICM order (matching the oracle's
// body_id). A single-body part yields an empty interference/clearance report.
nlohmann::json doc_eval_interference(const std::string& doc_id);

// Evaluate — measure 1-2 resolved refs against the LAST body (port of evaluate.py::measure
// via worker.py::_eval_measure). Each ref is a face/edge/vertex selector. 1 ref ->
// single-entity measure (length/radius/area/point/volume); 2 refs -> pair measure
// (distance + optional angle). Throws OpError on no bodies / a dangling ref / a bad count.
nlohmann::json doc_eval_measure(const std::string& doc_id, const nlohmann::json& refs);

// Evaluate FIELD — per-vertex DRAFT-ANGLE field of the active body (port of evaluate.py::
// draft_field via worker.py::_eval_draft). Tessellates the body, computes per-vertex
// draft = degrees(arcsin(clip(n·pull,-1,1))), writes a SCLR "draft" chunk to a temp .aicm
// file and returns {"aicm_path": <path>}. body_id 1-based, <=0 = LAST. `pull` is the
// (unnormalized) pull direction; `angle_deg` is the informational draft threshold.
nlohmann::json doc_eval_draft(const std::string& doc_id, int body_id,
                              const std::array<double, 3>& pull, double angle_deg);

// Evaluate FIELD — per-vertex CURVATURE field of the active body (port of evaluate.py::
// curvature_field via worker.py::_eval_curvature). kind = "mean"|"gaussian"|"max". Re-meshes
// + evaluates BRepLProp_SLProps at each vertex's projected UV; writes a SCLR "curvature_<kind>"
// chunk to a temp .aicm and returns {"aicm_path": <path>}. body_id 1-based, <=0 = LAST.
nlohmann::json doc_eval_curvature(const std::string& doc_id, int body_id,
                                  const std::string& kind);

// Evaluate FIELD — per-vertex WALL-THICKNESS field of the active body (port of evaluate.py::
// thickness_field via worker.py::_eval_thickness). Inward-raycasts (-normal) from each vertex
// via a Moller-Trumbore raycaster (the oracle's deterministic fallback); writes a SCLR
// "thickness" chunk to a temp .aicm and returns {"aicm_path": <path>}. body_id 1-based, <=0 =
// LAST. Parity is PHYSICAL (tolerance band), not bit-exact (oracle uses trimesh BVH).
nlohmann::json doc_eval_thickness(const std::string& doc_id, int body_id);

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

// C18 interactive sketch mode — port of engine/doc/session.py sketch_* (warm-started
// over aicad::sketch::solve_sketch). One sketch is "entered" at a time (active_sketch
// on the session). enter/solve/drag/drag_commit/exit/overview/regions/probe.
//
// sketch_enter: resolve plane, cold-solve, store warm state -> {entities, statuses,
//   dof, plane_frame, status, conflicting, redundant, skipped_constraints,
//   solve_rejected, constraints}.
// sketch_solve: warm re-solve the active sketch -> same shape.
// sketch_drag: transient warm-started drag (NO revision bump) -> {entities, statuses,
//   solve_rejected, constraints} or {ok:false, error, rejected_handles}.
// sketch_drag_commit: persist dragged coords as one undoable sketch_edit_entity batch
//   -> the ops-response (+ constraints).
// sketch_exit: persist last-solved coords, clear active_sketch, rebuild -> ops-response.
// sketch_overview: read-only solved snapshot (never touches active_sketch).
// sketch_probe: over-define pre-flight on a copy (read-only) -> {ok, status,
//   dof_before, dof_after, converged, conflicting, redundant, would_over_define}.
// sketch_regions: read-only closed-loop region enumeration -> {plane_frame, bbox_diag,
//   regions:[{index, centroid, area, depth, is_material, outer, holes, tris}]}.
nlohmann::json doc_sketch_enter(const std::string& doc_id, const std::string& sketch_fid);
nlohmann::json doc_sketch_solve(const std::string& doc_id, const std::string& sketch_fid);
nlohmann::json doc_sketch_overview(const std::string& doc_id, const std::string& sketch_fid);
nlohmann::json doc_sketch_regions(const std::string& doc_id, const std::string& sketch_fid);
nlohmann::json doc_sketch_drag(const std::string& doc_id, const std::string& sketch_fid,
                               const std::string& handle, const nlohmann::json& xy,
                               const nlohmann::json& targets);
nlohmann::json doc_sketch_drag_commit(const std::string& doc_id, const std::string& sketch_fid);
nlohmann::json doc_sketch_exit(const std::string& doc_id, const std::string& sketch_fid);
nlohmann::json doc_sketch_probe(const std::string& doc_id, const std::string& sketch_fid,
                                const nlohmann::json& action);

// sketch_project_ref: read-only projection of a datum plane / planar face / model edge
// into the sketch's (u,v) plane (engine/doc/session.py sketch_project_ref). `ref` is
// {"datum":...} or {"kind":"face|edge","selector":{...}}; returns {entity, source}.
// face/edge selectors resolve against the LAST solid body (single-body parts; the
// oracle's multi-body best-match is deferred). Throws OpError/unsupported_ref.
nlohmann::json doc_sketch_project_ref(const std::string& doc_id, const std::string& sketch_fid,
                                      const nlohmann::json& ref);

}  // namespace aicad::doc
