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
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>
#include <functional>
#include <cstdint>

#include <nlohmann/json.hpp>

#include "geom/aicm.h"  // aicm::AicmBody (doc_tessellate_bodies)

namespace chudware::doc {

// A contract violation (mirrors engine.doc.ops.OpError) -> the host's structured
// error envelope {code, message, detail}. Thrown across doc_* and converted at the
// libchudware boundary into a nonzero return carrying that envelope.
struct OpError : std::runtime_error {
    std::string code;
    nlohmann::json detail;
    OpError(std::string c, const std::string& msg, nlohmann::json d = nlohmann::json::object())
        : std::runtime_error(msg), code(std::move(c)), detail(std::move(d)) {}
    nlohmann::json envelope() const {
        return nlohmann::json{{"code", code}, {"message", what()}, {"detail", detail}};
    }
};

// CRASH RECOVERY (P0 deadlock fix). Every doc_* RPC takes the target document's
// per-doc mutex via DocLock, which releases correctly when a C++
// exception (e.g. OpError from a rejected op batch) unwinds the stack. But a
// WINDOWS STRUCTURED EXCEPTION (an access violation — null deref reading 0x60 —
// raised deep inside compile_document while the lock is held) does NOT run C++
// destructors under /EHsc, so the DocLock never releases and the mutex stays
// owned by the worker thread. The NEXT doc_* call on that same thread then
// re-locks the still-held non-recursive mutex => std::system_error EDEADLK
// ("resource deadlock would occur"), bricking that document until restart.
//
// The libchudware C dispatch boundary catches such a structured exception with an
// __except filter and calls this to drop the orphaned lock(s) BEFORE returning
// the error, so the thread can serve the next call cleanly. Idempotent and safe
// to call when no lock is held by this thread. Returns true if it released any.
bool force_release_doc_lock();

// Nonblocking lifecycle census. False means a document/store was busy; counters
// remain known lower bounds. Includes cache tasks through final publication.
bool activity_counts(std::uint64_t& cache_verification, std::uint64_t& dirty_documents);

// Create a new blank document ("part"; "assembly" not yet supported). Returns the
// ops-response + "doc_id". Registers the session in the process-global store.
//
// `resume_if_open` opts into ATOMIC resume-or-mint. The name check and the mint are
// otherwise two separate visits to the session registry, so two concurrent callers
// asking for the same name both see it free and both mint (live 2026-07-29,
// run-42508173312214: one new_part("lexus_f_keychain") in the trace, three tabs in the
// GUI). With the flag set, an already-open document of that name is returned as-is
// under the same lock that guards the uniquify scan — {"reused":true, "feature_count":N}
// plus "duplicate_doc_ids" when several answer to the name. Callers that mean "the part
// called X" (services/mcp new_part) pass true; callers that mean "another document"
// (new_part(fresh=True), import, reverse-engineering) leave it false and keep uniquifying.
// `project_dir`, when non-empty, limits the resume to documents without a file or whose
// file lies inside it: another project's open document is never resumed. It must be
// absolute (a relative value throws OpError("invalid_op")). A new assembly also uses it as
// the base folder for relative component sources until the document has a file.
nlohmann::json doc_new(const std::string& kind, const std::string& name,
                       bool resume_if_open = false, const std::string& project_dir = {});

// Apply an op batch. `base_revision` < 0 skips the optimistic-concurrency guard;
// otherwise a mismatch throws OpError("stale_revision"). A bad op throws OpError.
// `coalesce` folds the batch into the previous undo step (no new snapshot).
// `with_tess=false` omits mesh publication/response data, retaining rebuild diagnostics.
// A compile rejection preserves the committed tree/revision and tessellation;
// rejected_tree and rebuild.feature_status describe the rejected candidate.
nlohmann::json doc_apply_ops(const std::string& doc_id, long base_revision,
                             const nlohmann::json& ops, bool coalesce, bool with_tess = true);

// Stateless dry-run validation (engine.doc.ops dry_run, used by the agent modeler/
// fixer path). Copies the live tree and mutable compile-cache index into an
// unregistered scratch session; unchanged cached prefixes are not replayed.
// Never mutates the live session, publishes meshes, or notifies dependents.
// Returns {ok, source_revision, rebuild} with the candidate rebuild diagnostics
// (including in-band failures), or {ok:false, error} for structural exceptions.
// Failure error strings retain the ops_emit.dry_run composition.
// budget_ms > 0 opts advisory callers into POSIX process isolation for the entire
// candidate (all probes + response serialization). Timeout kills/reaps the child
// and returns ok:false, skipped:budget_exceeded, still_running:false. No fallback.
// Unsupported platforms return skipped:isolation_unavailable. Source snapshot/
// document-lock and transport waits are not a hard end-to-end wall-clock bound.
// Zero preserves the complete unbudgeted modeler/fixer validation path.
nlohmann::json doc_dry_run_ops(const std::string& doc_id, const nlohmann::json& ops,
                                int budget_ms = 0);

nlohmann::json doc_undo(const std::string& doc_id);     // bumps revision +1
nlohmann::json doc_redo(const std::string& doc_id);     // bumps revision +1
// Clear undo_stack + redo_stack (+ disk journal) for a doc without bumping
// revision. Used after a human (gui) doc_save so interactive undo resets at the
// saved baseline. Returns {"ok": true, "doc_id": ...}.
nlohmann::json doc_clear_history(const std::string& doc_id);
nlohmann::json doc_tree(const std::string& doc_id);     // no rebuild, no rev bump
nlohmann::json doc_rebuild(const std::string& doc_id);  // forced rebuild, no rev bump
// Cheap tab-switch activation (viewport switch perf): serve the session's read-cache
// compile (`compiled` at `compiled_rev == revision` — kept warm by every mutating op's
// warm_and_respond) as a full ops-response with a freshly published tess.aicm_path,
// WITHOUT recompiling or re-tessellating. Falls back to doc_rebuild's forced path when
// the cache is cold/stale (fresh daemon restart, disabled read cache). Assembly docs
// reuse the retained asm_result the same way. Never bumps the revision; never mutates.
nlohmann::json doc_activate(const std::string& doc_id);
bool doc_close(const std::string& doc_id);

// Live source tree + equation env for an associative re-pull by a drawing / derived
// consumer (mirrors engine/doc/drawing.py pulling an OPEN source DocSession's current
// bodies on every fetch). Returns {"tree":<doc json>, "env":<equation env>,
// "revision":<long>}; returns a null json when `doc_id` is not an open session, so the
// caller can fall back to its last good snapshot instead of crashing.
nlohmann::json doc_live_source(const std::string& doc_id);
// Standalone .draw+ source restoration: find an already-open source by durable
// path, or register an embedded part/assembly document as a live source session.
std::string doc_find_open_by_path(const std::string& path);
nlohmann::json doc_open_embedded(const nlohmann::json& document);


// C20 assembly RPCs (port of engine/doc/session.py solve_mates / set_explode_state /
// asm_world_bodies). doc_solve_mates re-solves the mates (optional drag {component,
// target[3]} soft-target) and returns the ops-response carrying doc_status
// (dof/over_defined/mate_status). Only a solve that changes the document (a component
// moves beyond the solver tolerance, reference frames bind, or component geometry
// differs) persists the solved transforms and bumps revision/dirty/undo; a no-op solve
// leaves all three untouched. Throws OpError("invalid_op") on a non-assembly doc.
nlohmann::json doc_solve_mates(const std::string& doc_id,
                               const nlohmann::json& drag = nlohmann::json(nullptr));
// Toggle the exploded-view state and re-tessellate (no revision bump — view-only).
nlohmann::json doc_set_explode_state(const std::string& doc_id, bool active, double factor);
// Set this document's DISPLAY tessellation tier ("draft"|"standard"|"fine") and
// re-tessellate (no revision bump — view-only, like doc_set_explode_state). Drops the
// document's compile + per-body mesh caches so every body re-meshes at the new
// deflections (geom::display_deflections). Returns the ops-response plus
// {ok, quality, triangles}; OpError("invalid_op") on an unknown quality name.
nlohmann::json doc_set_display_quality(const std::string& doc_id,
                                       const std::string& quality);
// World-placed component bodies (name + component + world bbox) for interference / BOM /
// drawings (`explode` applies the explode offset when the explode view is active).
nlohmann::json doc_asm_world_bodies(const std::string& doc_id, bool explode = false);

// Per-real-component BOM listing of an assembly: [{component, name, source, quantity}]
// with component-pattern instances ("<seed>#<k>") folded into their seed's quantity and
// the source path pulled from doc["components"][i].source.path. Returns an EMPTY array
// when `doc_id` is not an open assembly (so a part/closed source yields an empty BOM, not
// an error). Port of engine/doc/session.py::asm_component_bodies (BOM fields, no shape).
nlohmann::json doc_asm_bom_components(const std::string& doc_id);

// Enumerate the open C++ sessions: {"docs":[{doc_id,name,kind,path,revision,dirty}]}.
// Metadata only, no compile. Busy documents return their last committed summary
// with busy:true instead of waiting behind an in-flight operation.
nlohmann::json doc_list();

// Test-only synchronization seam; not an RPC. Runs body under the same per-doc
// lock as doc_apply_ops so contention regressions need no timing-sensitive geometry.
void with_doc_lock_for_testing(const std::string& doc_id, void (*body)(void*), void* context);

// PropertyManager-cancel checkpoints (engine.doc.session.checkpoint/revert).
// doc_checkpoint deep-snapshots the current doc and returns {"checkpoint": id, "revision": revision}.
// doc_revert restores the snapshot, pushes ONE undo entry of the pre-revert doc,
// rebuilds, bumps the revision, and returns the ops-response. Checkpoints are
// per-session and cleared on close. A bad id throws OpError("invalid_op").
// expect_revision >= 0 guards against reverting another writer's edits: a mismatch
// consumes the checkpoint and throws OpError("stale_revision") without changing the doc.
nlohmann::json doc_checkpoint(const std::string& doc_id);
nlohmann::json doc_revert(const std::string& doc_id, const std::string& checkpoint_id,
                          long expect_revision = -1);

// Copy/paste-as-derived live re-pull (engine.doc.session.refresh_derived). Re-bundles each
// derived group's source feature (+ closure) from the LIVE source session and splices it back
// over the existing derived copies in place. `id` empty == refresh ALL derived groups; else
// narrow to the group feature `id` belongs to (a non-derived id throws OpError("invalid_op")).
// Mutating on any change (one undo step + revision bump). A closed source / deleted source
// feature leaves the group at last-good and stamps derived_stale (never crashes). Returns the
// ops-response with an extra `derived`:{groups:[{group,status,...}]} summary. The copy/paste
// ops themselves (copy_feature / paste_feature / paste_feature_as_derived) run through
// doc_apply_ops; copy_feature is a read-only batch (no revision bump).
nlohmann::json doc_refresh_derived(const std::string& doc_id, const std::string& id);

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

// Simulation studies (FEA/CFD setups). Stored as a top-level `studies` array in the
// document JSON so they persist inside the .step+ container automatically. The core
// treats each study as OPAQUE JSON — only the `id` is core-assigned — and NEVER
// rebuilds geometry, so these are cheap and work for both part and assembly docs.
// list is read-only ({studies, revision}); add/edit/delete push one undo step, bump
// the revision, mark the doc dirty, and return {studies, revision} (add also
// study_id). A bad doc_id throws OpError("unknown_doc"); a missing study on
// edit/delete throws OpError("invalid_op").
nlohmann::json doc_sim_study_list(const std::string& doc_id);
nlohmann::json doc_sim_study_add(const std::string& doc_id, const nlohmann::json& study);
nlohmann::json doc_sim_study_edit(const std::string& doc_id, const std::string& study_id,
                                  const nlohmann::json& study);
nlohmann::json doc_sim_study_delete(const std::string& doc_id, const std::string& study_id);

// Resolve a viewport pick (1-based body ordinal + kind + 1-based MapShapes topo id)
// into a selector json {selector: {...}}. Throws OpError("unknown_doc"/...).
nlohmann::json doc_resolve_pick(const std::string& doc_id, int body_id,
                                const std::string& kind, int topo_id);

// The edge topo_ids bordering a face (for "fillet a whole face"). Returns {edges:[...]}.
nlohmann::json doc_face_edges(const std::string& doc_id, int body_id, int face_topo_id);

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
// Empty or relative `path` → server temp under <tmp>/chudware_cpp_docs/export_<doc>_<ts>.ext
// (absolute paths are honored unchanged). Throws OpError("export_failed", ...) on empty.
nlohmann::json doc_export_step(const std::string& doc_id, const std::string& path);

// doc_tessellate_bodies: tessellate the document's surviving solids and return them as AICM
// bodies (flat pos/idx in WORLD mm). Used by the agent CFD path (voxelize -> wind tunnel) and the
// preview renderer. Returns an empty vector on no bodies / failure (never throws).
std::vector<aicm::AicmBody> doc_tessellate_bodies(const std::string& doc_id);

// doc_render_png: render the document's surviving solids to a small isometric shaded PNG at
// `out_path` (a lightweight software rasterizer — no GL). Used by the agent's vision review so
// the model can SEE its current model and self-correct. Returns {"ok": bool, "path": <out>}.
// Fail-soft: {"ok": false} on no bodies / tessellation or render failure (caller falls back to
// text-only review). Never throws.
// annotate=false renders a CLEAN product shot (no face callouts / orientation triad / orientation
// tint) so the true material colour shows — used for parts-library thumbnails. Default true keeps
// the legacy annotated iso for the agent vision review.
// `background`, when it is a 3-element [r,g,b] array in 0-255, is the colour the frame is
// cleared to; omitted / null / malformed keeps the engine's flat 245 light grey, so an
// existing caller's pixels are unchanged. Shading, tinting and overlays are untouched — this
// replaces colour-keying the constant grey out of a finished frame.
nlohmann::json doc_render_png(const std::string& doc_id, const std::string& out_path,
                              bool annotate = true,
                              const nlohmann::json& background = nlohmann::json());

// doc_render_view: render ONE on-request view to a labelled PNG (task 03). `view` is a view spec:
// a named view ("front"|"back"|"top"|"bottom"|"left"|"right"|"iso"), an {"azimuth","elevation"}
// angle in degrees, or an explicit {"dir":[x,y,z]} camera direction (scene -> camera). The image is
// labelled with the resolved view name + bbox dims. Back-compatible (doc_render_png is unchanged).
//
// The object form additionally accepts CLOSE-UP framing, so a feature review is a real render
// rather than a magnified 30-px patch of a whole-part shot:
//   "width" / "height"  image size in px (default 512x400, clamped 64..2048)
//   "focus"             {"min":[x,y,z],"max":[x,y,z]} or {"center":[x,y,z],"radius":r} — the
//                       world AABB the camera frames instead of the whole model. Also
//                       "margin" (fraction of the box's projected size, default 0.15) and
//                       "clip_near" (drop everything in FRONT of the box — a deterministic
//                       cutaway that sees into a bore without a separate section op).
// Returns {"ok", "path", "view", "dims", "dir":[x,y,z], "frame"} where dir is the resolved
// scene->camera direction and `frame` is the exact orthographic projection used
// ({center,right,up,dir,scale,cu,cv,width,height,focused,clip_near}), so an overlay can
// reproject world points onto these pixels instead of re-deriving the camera and missing.
// Fail-soft; never throws.
// `annotate=false` suppresses the orientation tint, face callouts and axis triad for a clean
// close-up (it was previously accepted by callers and silently ignored).
// `background`: optional [r,g,b] 0-255 frame clear colour (see doc_render_png).
// `view.section` (object form): {"point":[3],"normal":[3],"ghost":true,"hatch":true} keeps the
// half-space dot(p-point, normal) <= 0 and draws a hatched cap, a 2 px cut outline and a ghost of
// the removed half; echoed as `section`. Absent/null leaves the pixels byte-identical.
nlohmann::json doc_render_view(const std::string& doc_id, const std::string& out_path,
                               const nlohmann::json& view, bool annotate = true,
                               const nlohmann::json& background = nlohmann::json());

// doc_view_metrics: measure what each of N exact perspective cameras (camera_pose.v1, the dict
// doc_render_view accepts at view.camera) actually sees of the doc's display mesh — per-face
// visible px / true visible area / mean |cos| / projected px, per-B-rep-edge visibility,
// contrast and silhouette, plus background/cap/depth-edge pixel counts and normal entropy.
// `params`: {doc_id, width=192, height=144 (clamped 32..1024), cameras:[...], section?,
// sections?}. Same projection/clipping/depth rule as doc_render_view; cameras measured in
// parallel. Refuses by name ({ok:false, code, reason}); never falls back to a default view.
// Contract: contracts/worker_rpc.md "doc_view_metrics".
nlohmann::json doc_view_metrics(const nlohmann::json& params);

// doc_render_contact_sheet: render SEVERAL views and tile them into one labelled PNG (task 03) —
// the multi-view self-check artifact. `views` is an array of view specs (see doc_render_view);
// empty/omitted defaults to front+right+top+iso. Each tile is headed with its view name + dims.
// Returns {"ok", "path", "views":[names], "cols", "rows", "tile":[w,h]}. Fail-soft; never throws.
// `background`: optional [r,g,b] 0-255, applied to every tile (gutters stay dark grey).
nlohmann::json doc_render_contact_sheet(const std::string& doc_id, const std::string& out_path,
                                        const nlohmann::json& views,
                                        const nlohmann::json& background = nlohmann::json());

// drawing_render_png: rasterize a 2D DRAWING document (its projected views) to a PNG at
// `out_path` so the AI can SEE and self-review a drawing the same way it does a 3D part
// (Bug D-1). Visible edges black, hidden edges grey, on white paper, auto-fit. Returns
// {"ok": bool, "path", "visible", "hidden"}. Fail-soft on a non-drawing doc / no views.
nlohmann::json drawing_render_png(const std::string& doc_id, const std::string& out_path);

// doc_face_points: resolve the face `selector` (kind=="face") against the LAST body's
// faces and sample that face's triangulation nodes in WORLD space, down-sampled to at
// most `max_n` points (uniform stride). Returns {"points": [[x,y,z],...], "tol_mm":
// max(0.5, diag*0.02)}. Throws OpError on a non-face selector / no bodies / a dangling
// face / no triangulation nodes (falling back to the selector centroid first).
nlohmann::json doc_face_points(const std::string& doc_id, const nlohmann::json& selector,
                               int max_n = 400);

// doc_query: run a semantic geometry query (doc/picks::resolve_geom_query) against the
// LAST body so an agent can introspect a part it did not build before editing it —
// "what faces/edges are here?". `query` is the {"of":"edge"|"face",...predicates} object.
// Returns {"count": n, "matches": [<selector>, ...]} where each selector carries the
// sub-shape's surf/centroid/axis/area_or_len (ready to paste into a fillet/hole/etc.).
nlohmann::json doc_query(const std::string& doc_id, const nlohmann::json& query);

// doc_adjacency (GSL W3): the face-adjacency graph the read-back never exposed. For the LAST
// body, returns {"faces":[{ord, surf, side, concavity, neighbors:[ord,...]}], "edges":[{between:
// [fa,fb], convex:bool, surf, length}]}. `ord` is the describe_faces enumeration index (== the
// query-match position), so "the edge between the top and front face" and "the faces this pocket
// cut" become first-class. Convexity is the signed dihedral between the two incident faces'
// outward normals at the shared edge (convex = a real corner, concave = an inside/pocket edge).
nlohmann::json doc_adjacency(const std::string& doc_id);

// doc_set_meta / doc_get_meta: generic, opaque, top-level document metadata persisted INSIDE the
// .step+ exactly like `studies` (the C++ never interprets it). The intake subsystem stores its
// MeasuredPartProfile / ProcessProfile / CriticalDimensionLedger / FitReport / IntakeState under
// keys here so an MCP-built part reopens in-app with its references intact. set returns {ok,key};
// get returns {ok, key, value|null}. Keys live under doc["meta"][<key>].
nlohmann::json doc_set_meta(const std::string& doc_id, const std::string& key,
                            const nlohmann::json& value);
nlohmann::json doc_get_meta(const std::string& doc_id, const std::string& key);

// doc_set_name: set the document display name (doc["name"]) under the doc lock so
// doc_tree / Feature Manager / viewport title see it. Does NOT bump revision or
// touch geometry. Empty/whitespace name is a no-op. Returns {ok:true, name:<applied>}.
nlohmann::json doc_set_name(const std::string& doc_id, const std::string& name);

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
// moments[3], principal_axes[3][3], material{density_g_cm3, source, key?}}. A surface body
// returns the {is_surface, area_mm2, ...} report. Throws OpError on no bodies / a bad body_id.
// density_g_cm3 nullopt = each body's applied material (body override, document material,
// assembly component material), 7.85 steel only where none resolves; a value overrides all.
nlohmann::json doc_eval_mass(const std::string& doc_id, int body_id,
                             std::optional<double> density_g_cm3 = std::nullopt);

// Evaluate — least-squares CYLINDER AXIS of the body's surface near a world point: the
// mating primitive for merging independently sourced CAD, where the imported body has no
// parametric feature whose axis could be read. Tessellation vertices within
// search_radius_mm of `near` are fed to geom::fit_cylinder. body_id is 1-based; <= 0
// selects the LAST body. Returns {axis_point[3], axis_dir[3] (unit), radius_mm,
// rms_residual_mm, n_points_used}. Throws OpError on no bodies / a bad body_id / a
// non-positive radius / fewer than 6 culled points / a fit that did not converge.
nlohmann::json doc_fit_cylinder_axis(const std::string& doc_id, int body_id,
                                     const std::array<double, 3>& near_pt,
                                     double search_radius_mm);

// doc_bbox: precise world AABB of the whole document. Returns {min[3], max[3], size[3],
// center[3]} in mm. Throws OpError on no bodies / an empty box.
nlohmann::json doc_bbox(const std::string& doc_id);

// Evaluate — geometry-validity report of the active body (port of evaluate.py::check via
// worker.py::_eval_check). Runs BRepCheck_Analyzer + free-edge / tiny-edge / sliver-face
// census. Returns {"issues":[{kind,severity,text}]} (empty issues == valid). body_id is
// 1-based; <= 0 selects the LAST body. Throws OpError on no bodies / a bad body_id.
nlohmann::json doc_eval_check(const std::string& doc_id, int body_id);

// Per-body selectable face/edge findings; omitted size thresholds scale with each
// body's bounds. IDs are the AICM picker TopExp::MapShapes indices (1-based).
nlohmann::json doc_eval_geometry_analysis(const std::string& doc_id,
                                          const nlohmann::json& options);

// Evaluate — SOLID HEALTH census of one or every body: the counts that justify a watertight
// verdict, with the stable sid of every offender. Returns {bodies:[{body_id, watertight,
// is_watertight, sound, faces, edges, vertices, shells, solids, free_edges:{count,sids},
// non_manifold_edges:{count,sids}, tiny_edges:{count,sids}, sliver_faces:{count,sids},
// seam_edges, degenerate_edges, orientation:{checked,consistent,…}, brep_valid, volume_mm3,
// volume_positive, area_mm2, self_intersection:{checked,self_intersecting,
// interference_count,kinds,elapsed_ms}}], body_count, all_watertight, all_sound}.
// Free-edge counting is seam/degenerate-aware (same rule as dress_is_closed_solid), so a
// cylinder's seam is NOT reported as a hole. Self-intersection is a real BOPAlgo_CheckerSI
// run at level 9 under a 20 s per-body wall-clock budget; a body it flags is NOT sound.
// A check that could not run (timeout, OCCT failure) reports checked:false with a reason
// and leaves `sound` alone — never a passing verdict for something not computed. body_id is
// 1-based; <= 0 reports EVERY body. Throws OpError on no bodies / a bad body_id.
nlohmann::json doc_eval_solid_health(const std::string& doc_id, int body_id);

// Exhaustive world-solid validity and geometric proximity census, including solids
// inside assembly components. Connectivity is geometric, not attachment/load-path proof.
// Incomplete native checks are explicit failures, never an empty passing inventory.
nlohmann::json doc_eval_model_integrity(const std::string& doc_id,
                                       const nlohmann::json& params);

// Evaluate — additive cross-section properties of all world-placed document solids.
// Independent bodies are not Boolean-unioned. `plane` is a reserved planar datum id
// ("plane.top/front/right"), a {"kind":"datum","feature":<id>} selector, or an arbitrary
// cut {"origin":[x,y,z],"normal":[nx,ny,nz]} (optional "xdir":[3]).
// Returns {area_mm2, centroid[3], Ixx, Iyy, Ixy}: global centroid and plane-local
// centroidal moments. A genuine empty intersection returns zero fields. A closed shell is
// sectioned as the solid it bounds. A plane that cuts a body with free edges throws
// section_failed with detail.reason "body_not_closed_solid" and body_id; a plane that
// misses such a body skips it. Failed section/face/integration throws section_failed,
// never partial or empty success. Missing geometry and invalid planes also throw OpError.
nlohmann::json doc_eval_section_props(const std::string& doc_id, const nlohmann::json& plane_ref);

// Evaluate — mirror-symmetry of the active body about `plane` (port of evaluate.py::
// symmetry via worker.py::_eval_symmetry). Returns {symmetric, deviation_mm3} where
// deviation is the symmetric-difference volume. body_id 1-based, <= 0 = LAST. `plane` as
// for section_props. Throws OpError on no bodies / a bad body_id / a bad plane.
nlohmann::json doc_eval_symmetry(const std::string& doc_id, int body_id,
                                 const nlohmann::json& plane_ref);

// Evaluate — pairwise interference + clearance across ALL surviving bodies (port of
// evaluate.py::interference via worker.py::_eval_interference, part-doc branch). Returns
// {ok, summary, parts:[{name,volume,bbox}], interferences:[{a,b,volume,overlap_volume_mm3,
// overlap_bbox,overlap_mesh}], clearances:[{a,b,distance}], contacts:[{a,b,distance}]}.
// Body names are "b1".."bN" in AICM order (matching the oracle's body_id); assembly docs
// use component names. A single-body part yields an empty interference/clearance report.
// `params` (all optional): `include_coincident` (bool, default false) additionally
// classifies a near-zero-clearance pair (distance <= 1e-6) as a `contacts` entry instead
// of a `clearances` one (SW-parity "coincident faces" gap); `min_clearance_mm` (default 0)
// tags any clearance entry below it with `"warning":"below min clearance"`; `members`
// (array of part-name strings) restricts pairwise checks to those names (case-sensitive
// exact match; absent/empty = all; malformed/unknown names are refused). `scope` records
// all/member scope and selected/total counts; `complete` applies ONLY to that scope.
// Each interferences[] item also carries
// `overlap_volume_mm3` (same as volume), `overlap_bbox` [[min],[max]] world mm, and
// `overlap_mesh` {positions:[x,y,z,...], indices:[i0,i1,i2,...]} (empty arrays on mesh fail).
// Imported compound members are exploded to SOLID(s) before the Common op.
nlohmann::json doc_eval_interference(const std::string& doc_id,
                                     const nlohmann::json& params = nlohmann::json::object());

// Evaluate — pairwise FEATURE-tool overlap WITHIN one part (port of the new WS-B
// intra-part check; no Python analogue). Pairs every unordered combination of
// CompileResult::tool_recs (the pre-boolean tool solid each boss/cut/hole/pattern/
// mirror feature contributed — see ToolRec in document.h) and runs BRepAlgoAPI_Common
// to measure shared volume. `params` (all optional): `features` (array of feature-id
// strings) restricts pairing to combinations touching at least one listed id (bounds
// add_features' latency to new-batch x existing-tree); `min_volume_mm3` (default
// 1e-3, mirrors interference's INTERFERENCE_EPS) floors a reportable overlap. Kind is
// "boss_overlap" (add+add — two additive features share volume, often intentional),
// "cut_overlap" (cut+cut — redundant removal), "cut_into_boss" (add+cut, partial), or
// "boss_swallowed" (add+cut where the common volume is >= 99% of the additive tool's
// volume — the cut removed the ENTIRE earlier boss, almost certainly an error).
// Also reports `dead_features`: ok, non-suppressed features that contributed a tool
// but own no surviving face/edge (fully consumed downstream). Returns {ok, summary,
// pairs:[{a,b,a_name,b_name,kind,volume_mm3}] (volume desc), dead_features:[id,...]}.
// `ok` is false ONLY when a boss_swallowed pair exists — every other kind is a
// warning (overlapping bosses/cuts are common, intentional design).
nlohmann::json doc_eval_feature_overlap(const std::string& doc_id,
                                        const nlohmann::json& params = nlohmann::json::object());

// Evaluate — is each ADDITIVE feature's material seated on the rest of the part? Per add
// ToolRec solid (pattern/mirror: per instance) against the final part: X = part ∩ tool,
// Y = part − tool. Verdicts: "floating" (dist(X,Y) > gap tolerance — its own island),
// "barely_touching" (touches, but its best-supported face has < max(0.25 mm²,
// min_contact_frac × largest tool face) of backed area — an edge/point contact),
// "overhang" (the foot — the tool face with the largest SUPPORTED SHARE, ties by supported
// area then a mirror-invariant face key — has > overhang_frac of its area with no
// material behind it, probed along the outward normal). The first solid-producing feature
// and authored mode:"new"/merge:false features are skipped (the latter in `independent`).
// `params` (optional): features [ids], overhang_frac (0.05), min_contact_frac (0.02),
// budget_s (10). Returns {ok, summary, solid_count, features:[flagged entries {feature_id,
// name, type, instance?, verdict, detail, location[3], gap_mm, contact_area_mm2,
// base_face_area_mm2?, base_face_center[3]?, base_face_normal[3]?, supported_area_mm2, supported_frac?, overhang_frac?,
// overhang_area_mm2?, overhang_mm?, region?{min,max}, nearest_point?, nearest_point_on_part?,
// tool_volume_mm3, kept_volume_mm3}], flagged:[ids], attached:[ids], attached_count,
// independent:[ids], not_checked:[ids], thresholds}. `ok` is false iff anything is flagged.
nlohmann::json doc_eval_feature_attachment(const std::string& doc_id,
                                           const nlohmann::json& params = nlohmann::json::object());

// C1 (WS-C reverse-engineering) — general shape-vs-shape comparison; see recon.h::
// compare_shapes for the full field/degraded-mode contract. `params`: `doc_a`, `doc_b`
// (required doc ids); `body_a`/`body_b` (optional — a "bN" name as doc_eval_interference's
// members use, 1-based AICM order; default = geom::fuse_many of every surviving solid body,
// matching doc_eval_interference's part-doc member collection). Both docs are compiled fresh
// (not the ensure_compiled cache) so a just-edited doc always compares its latest geometry.
// Throws OpError on an unknown doc_id, an unknown body name, or a doc with no bodies.
nlohmann::json doc_eval_compare(const nlohmann::json& params);

// PLM graphical diff: render the geometry DIFFERENCE between two revisions of a part to a PNG.
// `params`: `doc_a` (OLDER), `doc_b` (NEWER), `out_path` (required); `body_a`/`body_b` (optional
// "bN" names, exactly as doc_eval_compare); `view` (any resolve_view_spec form, default ISO);
// `width`/`height` (default 640x480, clamped to [64,4096]); `mode` — "context" (default: the
// unchanged bulk is drawn ghosted so an internal change is still visible) or "changes_only" (the
// unchanged bulk is omitted entirely).
//
// Recipe-vs-solved rationale: the PLM vault commits feature RECIPES, never solved B-rep, so a
// revision diff cannot subtract two stored meshes. Both sides are therefore re-solved fresh via
// eval_compare_pick_shape (same as doc_eval_compare — no ensure_compiled cache reuse) and the
// difference is computed by OCCT booleans on the freshly solved shapes.
//
// Colour semantics (fixed across engine/Python/GUI): added = B-A = green (0.16,0.72,0.32),
// removed = A-B = red (0.86,0.24,0.24), unchanged = A^B = grey (0.62,0.64,0.67) at alpha 96.
// Every view and both modes frame the camera on the A∪B fuse so images register pixel-for-pixel.
//
// Returns {ok, path, view, mode, match_pct, added_volume_mm3, removed_volume_mm3,
// unchanged_volume_mm3, added_regions, removed_regions, identical} and, on failure,
// {ok:false, degraded:true, reason} / {ok:false, error, code}. Never throws.
nlohmann::json doc_render_diff(const nlohmann::json& params);

// C2 (WS-C reverse-engineering) — convert-entities primitive; see recon.h::face_wires for
// the full per-edge classification + return-shape contract. `params.face` resolves against
// the LAST surviving body of `doc_id`, either as a selector/geom-query object (the same
// predicate-aware path doc_eval_measure's refs use) or as `{"sid": "<stable face handle>"}`
// (matched against make_selector's sid over the body's current faces — the handle GSL's
// doc_query/describe_part hand back). Throws OpError on no bodies, a missing/unresolved
// params.face, or a resolved-but-non-planar face.
nlohmann::json doc_face_wires(const std::string& doc_id, const nlohmann::json& params);

// Evaluate — measure 1-8 refs in world space. Picked refs carry
// {body_id, selector}; the body ID is the AICM identity, not a vector index. 1 ref ->
// single-entity measure (length/radius/area/point/volume) plus `anchor` [x,y,z] centroid
// and (for edge/face) `polyline` [[x,y,z],...] <=128 pts; 2 refs -> pair measure
// (distance + optional angle) with per-ref enrichment under `a`/`b`; 3+ -> multi_distance
// with per-ref `elements[]`. `resolved_refs[]` carries world-space selector readbacks.
// Throws OpError on missing bodies, invalid owners, stale refs or an invalid count.
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

// Export the document's surviving solid bodies to `path` in `fmt` (step/iges/brep/stl/
// obj/ply/3mf/gltf/glb/3dm/dxf/dwg/sat) via OCCT. For mesh formats, the tessellation quality
// is controlled by lin_deflection (mm, or a fraction when `relative`), ang_deflection
// (radians), and `ascii` (STL container); the defaults reproduce the historical
// behaviour. dxf/dwg/sat write exact ACIS solids in `units` ("mm"/"in"/... or an $INSUNITS
// code; empty = the drawing units recorded by the doc's DWG/DXF/SAT import, else mm) and
// are re-read to prove the solids survive. Returns {"path": <abs>, ...}. Throws
// OpError("export_failed") on no bodies / an unsupported format / a writer failure, and
// OpError("invalid_op") on `units` for another format.
nlohmann::json doc_export_file(const std::string& doc_id, const std::string& path,
                               const std::string& fmt, double lin_deflection = 0.05,
                               double ang_deflection = 0.35, bool relative = false,
                               bool ascii = false, const std::string& units = std::string());

// Count the triangles/vertices a mesh export of this document would produce at the given
// tessellation quality, without writing a file. Returns {triangles, vertices, bodies}.
// Used by the GUI export dialog's live estimate.
nlohmann::json doc_export_estimate(const std::string& doc_id, double lin_deflection = 0.05,
                                   double ang_deflection = 0.35, bool relative = false);

// Import a neutral B-rep file (step/iges/brep) as a new `import` feature. With `into`
// (a doc id) it appends to that session; otherwise it creates a new part session.
// Returns the ops-response + "doc_id" (engine.doc.formats import_file).
//
// `body_select` may carry `keep_bodies` / `exclude_bodies` (arrays of 1-based body
// ordinals or body-name strings); they are copied verbatim onto the import feature's
// params, where the compiler's sub-selection filter reads them. Editing those params
// later (edit_feature) re-filters on the next recompute.
nlohmann::json doc_import_file(const std::string& path, const std::string& into,
                               bool as_solid, const std::string& fmt,
                               const nlohmann::json& body_select = nlohmann::json::object());

// Insert a registered supplier/vendor CAD part as an `import` feature. Reads the Python-written
// vendor registry index (%APPDATA%/Chudware/vendor_parts/index.json), resolves the cached CAD path +
// metadata for slug(supplier)::slug(part_number), and applies an import op carrying the vendor_*
// params. `into` empty -> mint a new part doc. Port of engine/vendor_parts.py::_vendor_part_insert.
nlohmann::json doc_vendor_part_insert(const std::string& into, const std::string& supplier,
                                      const std::string& part_number, const std::string& name);

// STATELESS diagnostic: read a mesh file into its RAW per-object meshes (no sewing) and
// return {"objects": [{"name", "verts":[x,y,z,...], "tris":[i,j,k,...]}, ...]}. Used by
// the 3MF bit-parity gate to compare the C++ lib3mf extraction against the Python oracle
// (engine.doc.formats._import_3mf). `fmt` defaults to detect_format(path) when empty.
nlohmann::json mesh_read_raw(const std::string& path, const std::string& fmt);

// The import/export format matrix (port of engine/doc/formats.py::doc_formats). Returns
// {"formats": [{fmt, label, exts, lane, can_import, can_export}, ...]} sourced from
// format_registry() — the host builds its Open/Import/Export filters from this so the
// UI never lists a format the engine can't handle (and vice-versa). Stateless.
nlohmann::json doc_formats();

// Persist the session to `path` (or its current path if `path` is empty) as a
// .step+ container. Clears the dirty flag, records the path. Returns {"path": ...,
// "cache": {written, entries, skipped_reason?, checkpoints, ...}} (contracts/stepplus.md §9):
// the build state (display cache + per-feature compile checkpoints) is saved with the file.
// Throws OpError("io_error") if no path is available, or on a write failure.
// preserve_path writes a nonbinding snapshot from cached scratch state: the live
// path, dirty flag, tree and revision are unchanged, and snapshot-only relative
// component paths are resolved against the live destination before serialization.
nlohmann::json doc_save(const std::string& doc_id, const std::string& path,
                        bool preserve_path = false);

// Open a document at `path` into a FRESH session and return the ops-response + "doc_id" +
// "warnings" + "cache_hit"/"warm"/"cache_miss_reason". A valid saved cache hydrates the
// compiled state (no feature rebuild); otherwise the tree rebuilds. A native .step+
// container is read as a feature tree; ANY OTHER
// file (.step/.iges/.stl/.obj/…) is IMPORTED as a fresh part doc instead (so manual
// File>Open/Import of a neutral file works). `as_solid` promotes a closed mesh to a solid
// on import. Throws OpError("io_error") on a bad file. `detach_path` is for opening
// autosave/recovery artifacts: the doc forgets `path` (so a later save can't land in the
// autosave tier, which the recovery flow deletes right after opening) and the name is
// re-uniqued against the open docs (recovered duplicates would otherwise all be "Part1").
nlohmann::json doc_open(const std::string& path, bool as_solid = false,
                        bool detach_path = false);
// Called outside document locks after an unverified saved cache is re-solved.
void set_cache_refresh_sink(
    std::function<void(const std::string&, const nlohmann::json&)> sink);

// C18 interactive sketch mode — port of engine/doc/session.py sketch_* (warm-started
// over chudware::sketch::solve_sketch). One sketch is "entered" at a time (active_sketch
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

// Sketch completion suite.
//
// doc_sketch_auto_constrain: deterministic auto-coincident pass. Scans entity
//   endpoints, pairs those within tol_mm (default 1e-4) that are not already
//   coincident-constrained, and (apply=false) returns the proposals
//   [{a:{handle}, b:{handle}, gap_mm}], or (apply=true) adds a `coincident`
//   constraint per proposal PERSISTENTLY (one undoable batch), each guarded by an
//   add->analyze->rollback-on-conflict safety check (a proposal that turns the
//   system conflicting is skipped, NOT applied). Returns {applied, skipped_conflicting,
//   regions_before, regions_after, proposals}. `opts` = {tol_mm?, apply?}.
//
// doc_sketch_health: read-only narrated constraint-state report — closure (regions +
//   open chains with the gap-endpoint handles named), dof with the free variables in
//   plain words, per-entity under/full/over status, redundant/conflicting tags with
//   removal suggestions, and a heuristic ordered constraint schedule (geometric
//   constraints then driving dimensions). Also emits the same findings as frozen
//   aicad.advisory/1 items under "advisory". No mutation, no rebuild, no rev bump.
nlohmann::json doc_sketch_auto_constrain(const std::string& doc_id,
                                         const std::string& sketch_fid,
                                         const nlohmann::json& opts);
nlohmann::json doc_sketch_health(const std::string& doc_id, const std::string& sketch_fid);

// ---- sketch3d (3D free-space sketch, feature type "sketch3d") --------------
//
// Both are STATELESS reads over the persisted feature params (no active-sketch
// session state). Contract + request/response examples:
// contracts/sketch3d_gui_contract.md.
//
// doc_sketch3d_health: solve + report -> {ok, status:"under"|"full"|"over", dof,
//   converged, statuses:{eid:...}, redundant:[cid], conflicting:[cid],
//   free:[{entity,param,var}], entities:[{id,type,construction,...solved coords}],
//   wires:[{closed,n_edges}]} (wire order == the compiler's spine/guide order).
// doc_sketch3d_drag: constrained drag of a point handle ("e1.p2", "e2.pm",
//   "e3.p0"...) toward world xyz [x,y,z]; `targets` (list of {handle,xyz}) wins
//   over handle/xyz for multi-point drags. Returns {ok, entities:[...],
//   statuses, rejected_handles, converged, budget_exceeded}. TRANSIENT — commit
//   by sending doc_apply_ops edit_feature {params:{entities:[...solved...]}}.
nlohmann::json doc_sketch3d_health(const std::string& doc_id, const std::string& sketch_fid);
nlohmann::json doc_sketch3d_drag(const std::string& doc_id, const std::string& sketch_fid,
                                 const std::string& handle, const nlohmann::json& xyz,
                                 const nlohmann::json& targets);

// Read-only projection through the same whole-document resolver as sketch_convert.
// Refs: {datum:"front|top|right|axis.x|axis.y|axis.z|origin"},
// {kind:"face|edge|vertex",selector}, or {kind:"sketch_entity",sketch,entity}.
// Edge mode:"pierce" (optional near:[u,v]) returns an isolated plane crossing.
// Returns {entity, source}; a multi-entity source previews its first projected entity.
nlohmann::json doc_sketch_project_ref(const std::string& doc_id, const std::string& sketch_fid,
                                      const nlohmann::json& ref);

// sketch_project_entities ("Convert Entities" / external references): project a LIST of
// external refs (datum plane / face / model edge) onto the active sketch's (u,v) plane and
// return ready-to-insert doc-schema §3.1 REFERENCE entities (construction:true). Uses OCCT
// orthographic projection (GeomProjLib) + recon.cpp's curve classifier, so faces project all
// their boundary wires and tilted circles become ellipses correctly (the Convert-Entities fix).
// READ-ONLY: reuses the session compile cache, with no document/revision mutation.
// Ref grammar matches doc_sketch_project_ref. Returns
// {ok, entities, approx, sources:[{...,count}], plane_frame}. Use sketch_convert to
// insert geometry with an associative external-reference link.
nlohmann::json doc_sketch_project_entities(const std::string& doc_id,
                                           const std::string& sketch_fid,
                                           const nlohmann::json& refs);

// ---- Cross-document dependency registry (feat(xdoc)) -----------------------
// Process-global lightweight registry: maps source_doc_id → set of dependents.
// Each dependent records its own doc_id + dependency kind + the source revision
// at the time it was registered (stale when source.revision > linked_revision).
//
// Kinds (string, open vocabulary):
//   "cam"      — a CAM job doc that extracted geometry from this part
//   "sim"      — a sim study linked to this part
//   "drawing"  — a drawing view referencing this part
//
// doc_dep_register: upsert a dependency link. Idempotent: re-registering the same
//   (source, dependent, kind) triple with a newer linked_revision updates the
//   baseline. Returns {"registered": true}.
//
// doc_dep_query: enumerate all dependents of source_doc_id that are currently
//   stale (source.revision > linked_revision). Returns:
//   {"source_revision": N, "dependents": [{dep_doc_id, kind, linked_revision, stale}]}
//   `kind` empty → all kinds. Read-only; never blocks.
//
// doc_dep_notify: internal — called automatically after every successful rebuild
//   (doc_apply_ops, doc_undo, doc_redo). Stamps every registered dependent with a
//   stale flag so the next doc_dep_query shows it. Returns {"notified": count}.
//   Exposed here for testing and for the libchudware C ABI (cam_panel calls it
//   indirectly via the pull-model doc_list_open polling, but explicit notify gives
//   sub-frame latency for in-process callers).
nlohmann::json doc_dep_register(const std::string& source_doc_id,
                                const std::string& dep_doc_id,
                                const std::string& kind,
                                long linked_revision);
nlohmann::json doc_dep_query(const std::string& source_doc_id, const std::string& kind);
nlohmann::json doc_dep_notify(const std::string& source_doc_id, long new_revision);
nlohmann::json doc_dep_unregister(const std::string& dep_doc_id, const std::string& kind);

// Kinematic motion study: drive one angle/distance mate across [start, stop] in
// `steps` samples, solve the assembly mate graph on an immutable document snapshot,
// and collect per-body world transforms for every frame. Optionally checks
// interference and minimum-clearance violations at every sample. The live document,
// revision, undo history, mate expression, warm state, and solved pose are untouched.
//
// Returns {ok, doc_id, mate_id, driver:{type,unit,start,stop,steps},
//          trajectory:[{i,value,dof,converged,bodies:[{name,component,
//                      transform:[16 floats col-major]}]}],
//          interference:{checked, first_collision_step, first_collision_value, detail},
//          summary}. ok=false distinguishes any unresolved/non-converged frame.
// Throws OpError("invalid_op") on non-assembly doc, missing/non-drivable mate,
// non-finite range/clearance, or invalid dynamics setup.
//
// `dynamics`: pass a JSON OBJECT to additionally run rigid-body INVERSE dynamics
// over the swept trajectory (mass/inertia from body geometry x density, gravity,
// applied loads and viscous damping; per-mate reaction wrenches solved through the
// mate constraint-Jacobian wrench bases in doc/motion_dynamics.h).
// Keys (all optional): {duration_s (default 1.0 across the sweep),
//   gravity:[3] m/s² (default [0,0,-9.81]), default_density_g_cm3 (default 7.85),
//   densities:{component_id: g/cm³}, masses:{component_id: kg},
//   loads:[{component, force_n:[3], torque_nm:[3]}],
//   damping:{linear_ns_per_m, angular_nms_per_rad}}.
// The block includes {ok,force_path_resolved,has_nonzero_reaction,dt_s,duration_s,
//   bodies,joints,frames:[{i,t_s,force_residual_n,moment_residual_nm,balanced,
//   reactions:[{mate_id,component_a,component_b,force_n:[3],moment_nm:[3],
//   force_mag_n,moment_mag_nm}]}],per_joint_worst,per_joint_worst_moment,
//   worst_case,worst_moment_case,warnings}. Reactions act on component_b from
// component_a in world coordinates; force is N and moment is N·m.
// Pass null / omit for the field-compatible kinematics-only behavior.
// `drivers`: optional nonempty [{mate_id,start,stop}] array, authoritative over the
// legacy single-driver fields. Unique active angle/distance mates share the same
// normalized sample times. Drivers and live expressions/poses/history are not mutated.
// Adds drivers:[{mate_id,type,unit,start,stop,steps}], frame.driver_values:{id:value},
// and per-driver residual/relative-reference pose checks in frame.pose_check.bodies.
// frame.value and result.driver/mate_id remain the first driver's legacy projection.
// Interference adds complete, frames_checked (fully evaluated valid frames),
// pair_failures:[{i,a,b,component_a,component_b,reason}], geometry_failures.
// Every frame carries interference_complete, nullable interference, collisions and
// collision_detail (stable component_a/component_b plus names, overlap or distance).
// Incomplete geometry/checks never mean collision-free; first_collision_step stays null
// until a known hit. ok=false on incomplete requested checks, not on a known collision.
nlohmann::json doc_simulate_motion(const std::string& doc_id,
                                   const std::string& mate_id,  // "" = auto-pick
                                   double start, double stop, int steps,
                                   bool check_interference,
                                   double min_clearance_mm = 0.0,
                                   const nlohmann::json& dynamics = nlohmann::json(nullptr),
                                   const nlohmann::json& drivers = nlohmann::json(nullptr));

// --- doc-lock contention meters (per-doc locking) ----------------------------------
// Each modeling RPC locks the target document (DocLock → per-session mutex). Meters
// measure lock wait/hold/queue-depth. Cross-doc ops multi-lock in doc_id order; the
// registry mutex is short-lived (map pin only). Still the primary bottleneck signal
// when many subagents drive ONE engine (blocked-on-lock is invisible to on-CPU sampling).

// Stamp the calling thread's current RPC method name so DocLock can attribute wait/hold to
// the caller. The pointer must stay valid for the duration of the call (the dispatch boundary
// passes the method string's c_str, then clears it with nullptr on return).
void set_current_rpc_method(const char* method);

// Snapshot the contention meters as JSON (acquisitions, %contended, total/avg/max wait+hold,
// pct_wall_blocked, peak waiters, and the top methods by cumulative wait). reset=true zeroes
// the counters and restarts the window — call it right before a measured run.
nlohmann::json contention_report(bool reset);

}  // namespace chudware::doc
