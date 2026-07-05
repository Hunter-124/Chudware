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
#include <vector>

#include <nlohmann/json.hpp>

#include "geom/aicm.h"  // aicm::AicmBody (doc_tessellate_bodies)

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

// CRASH RECOVERY (P0 deadlock fix). Every doc_* RPC takes the process-global
// session mutex via a std::lock_guard, which releases correctly when a C++
// exception (e.g. OpError from a rejected op batch) unwinds the stack. But a
// WINDOWS STRUCTURED EXCEPTION (an access violation — null deref reading 0x60 —
// raised deep inside compile_document while the lock is held) does NOT run C++
// destructors under /EHsc, so the lock_guard never releases and the mutex stays
// owned by the worker thread. The NEXT doc_* call on that same thread then
// re-locks the still-held non-recursive mutex => std::system_error EDEADLK
// ("resource deadlock would occur"), bricking EVERY document until restart.
//
// The libaicad C dispatch boundary catches such a structured exception with an
// __except filter and calls this to drop the orphaned lock BEFORE returning the
// error, so the thread can serve the next call cleanly. Idempotent and safe to
// call when the lock is NOT held by this thread (it no-ops via try_lock probing).
// Returns true if it actually released an orphaned lock.
bool force_release_doc_lock();

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

// Live source tree + equation env for an associative re-pull by a drawing / derived
// consumer (mirrors engine/doc/drawing.py pulling an OPEN source DocSession's current
// bodies on every fetch). Returns {"tree":<doc json>, "env":<equation env>,
// "revision":<long>}; returns a null json when `doc_id` is not an open session, so the
// caller can fall back to its last good snapshot instead of crashing.
nlohmann::json doc_live_source(const std::string& doc_id);

// C20 assembly RPCs (port of engine/doc/session.py solve_mates / set_explode_state /
// asm_world_bodies). doc_solve_mates re-solves the mates (optional drag {component,
// target[3]} soft-target), persists the solved transforms, bumps the revision, and
// returns the ops-response carrying doc_status (dof/over_defined/mate_status). Throws
// OpError("invalid_op") on a non-assembly doc.
nlohmann::json doc_solve_mates(const std::string& doc_id,
                               const nlohmann::json& drag = nlohmann::json(nullptr));
// Toggle the exploded-view state and re-tessellate (no revision bump — view-only).
nlohmann::json doc_set_explode_state(const std::string& doc_id, bool active, double factor);
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
// Mirrors engine.doc.session.list_sessions (metadata only — no compile).
nlohmann::json doc_list();

// PropertyManager-cancel checkpoints (engine.doc.session.checkpoint/revert).
// doc_checkpoint deep-snapshots the current doc and returns {"checkpoint": id}.
// doc_revert restores the snapshot, pushes ONE undo entry of the pre-revert doc,
// rebuilds, bumps the revision, and returns the ops-response. Checkpoints are
// per-session and cleared on close. A bad id throws OpError("invalid_op").
nlohmann::json doc_checkpoint(const std::string& doc_id);
nlohmann::json doc_revert(const std::string& doc_id, const std::string& checkpoint_id);

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
// Throws OpError("export_failed", "document has no solid bodies to export") on empty.
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
nlohmann::json doc_render_png(const std::string& doc_id, const std::string& out_path);

// doc_render_view: render ONE on-request view to a labelled PNG (task 03). `view` is a view spec:
// a named view ("front"|"back"|"top"|"bottom"|"left"|"right"|"iso"), an {"azimuth","elevation"}
// angle in degrees, or an explicit {"dir":[x,y,z]} camera direction (scene -> camera). The image is
// labelled with the resolved view name + bbox dims. Back-compatible (doc_render_png is unchanged).
// Returns {"ok", "path", "view", "dims", "dir":[x,y,z]} where dir is the resolved scene->camera
// direction. Fail-soft; never throws.
nlohmann::json doc_render_view(const std::string& doc_id, const std::string& out_path,
                               const nlohmann::json& view);

// doc_render_contact_sheet: render SEVERAL views and tile them into one labelled PNG (task 03) —
// the multi-view self-check artifact. `views` is an array of view specs (see doc_render_view);
// empty/omitted defaults to front+right+top+iso. Each tile is headed with its view name + dims.
// Returns {"ok", "path", "views":[names], "cols", "rows", "tile":[w,h]}. Fail-soft; never throws.
nlohmann::json doc_render_contact_sheet(const std::string& doc_id, const std::string& out_path,
                                        const nlohmann::json& views);

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

// doc_bbox: precise world AABB of the whole document. Returns {min[3], max[3], size[3],
// center[3]} in mm. Throws OpError on no bodies / an empty box.
nlohmann::json doc_bbox(const std::string& doc_id);

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
// [{a,b,distance}], contacts:[{a,b,distance}]}. Body names are "b1".."bN" in AICM order
// (matching the oracle's body_id). A single-body part yields an empty interference/
// clearance report. `params` (B4, all optional): `include_coincident` (bool, default
// false) additionally classifies a near-zero-clearance pair (distance <= 1e-6) as a
// `contacts` entry instead of a `clearances` one (SW-parity "coincident faces" gap);
// `min_clearance_mm` (default 0) tags any clearance entry below it with
// `"warning":"below min clearance"`.
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

// C1 (WS-C reverse-engineering) — general shape-vs-shape comparison; see recon.h::
// compare_shapes for the full field/degraded-mode contract. `params`: `doc_a`, `doc_b`
// (required doc ids); `body_a`/`body_b` (optional — a "bN" name as doc_eval_interference's
// members use, 1-based AICM order; default = geom::fuse_many of every surviving solid body,
// matching doc_eval_interference's part-doc member collection). Both docs are compiled fresh
// (not the ensure_compiled cache) so a just-edited doc always compares its latest geometry.
// Throws OpError on an unknown doc_id, an unknown body name, or a doc with no bodies.
nlohmann::json doc_eval_compare(const nlohmann::json& params);

// C2 (WS-C reverse-engineering) — convert-entities primitive; see recon.h::face_wires for
// the full per-edge classification + return-shape contract. `params.face` resolves against
// the LAST surviving body of `doc_id`, either as a selector/geom-query object (the same
// predicate-aware path doc_eval_measure's refs use) or as `{"sid": "<stable face handle>"}`
// (matched against make_selector's sid over the body's current faces — the handle GSL's
// doc_query/describe_part hand back). Throws OpError on no bodies, a missing/unresolved
// params.face, or a resolved-but-non-planar face.
nlohmann::json doc_face_wires(const std::string& doc_id, const nlohmann::json& params);

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

// Export the document's surviving solid bodies to `path` in `fmt` (step/iges/brep/stl/
// obj/ply/3mf/gltf/glb) via OCCT. For mesh formats, the tessellation quality is
// controlled by lin_deflection (mm, or a fraction when `relative`), ang_deflection
// (radians), and `ascii` (STL container); the defaults reproduce the historical
// behaviour. Returns {"path": <abs>}. Throws OpError("export_failed") on no bodies / an
// unsupported format / a writer failure.
nlohmann::json doc_export_file(const std::string& doc_id, const std::string& path,
                               const std::string& fmt, double lin_deflection = 0.05,
                               double ang_deflection = 0.35, bool relative = false,
                               bool ascii = false);

// Count the triangles/vertices a mesh export of this document would produce at the given
// tessellation quality, without writing a file. Returns {triangles, vertices, bodies}.
// Used by the GUI export dialog's live estimate.
nlohmann::json doc_export_estimate(const std::string& doc_id, double lin_deflection = 0.05,
                                   double ang_deflection = 0.35, bool relative = false);

// Import a neutral B-rep file (step/iges/brep) as a new `import` feature. With `into`
// (a doc id) it appends to that session; otherwise it creates a new part session.
// Returns the ops-response + "doc_id" (engine.doc.formats import_file).
nlohmann::json doc_import_file(const std::string& path, const std::string& into,
                               bool as_solid, const std::string& fmt);

// Insert a registered supplier/vendor CAD part as an `import` feature. Reads the Python-written
// vendor registry index (%APPDATA%/AI-CAD/vendor_parts/index.json), resolves the cached CAD path +
// metadata for slug(supplier)::slug(part_number), and applies an import op carrying the vendor_*
// params. `into` empty -> mint a new part doc. Port of engine/vendor_parts.py::_vendor_part_insert.
nlohmann::json doc_vendor_part_insert(const std::string& into, const std::string& supplier,
                                      const std::string& part_number, const std::string& name);

// STATELESS diagnostic: read a mesh file into its RAW per-object meshes (no sewing) and
// return {"objects": [{"name", "verts":[x,y,z,...], "tris":[i,j,k,...]}, ...]}. Used by
// the 3MF bit-parity gate to compare the C++ lib3mf extraction against the Python oracle
// (engine.doc.formats._import_3mf). `fmt` defaults to detect_format(path) when empty.
nlohmann::json mesh_read_raw(const std::string& path, const std::string& fmt);

// Persist the session to `path` (or its current path if `path` is empty) as a
// .step+ container. Clears the dirty flag, records the path. Returns {"path": ...}.
// Throws OpError("io_error") if no path is available, or on a write failure.
nlohmann::json doc_save(const std::string& doc_id, const std::string& path);

// Open a document at `path` into a FRESH session, rebuild it, and return the ops-response
// + "doc_id" + "warnings". A native .step+ container is read as a feature tree; ANY OTHER
// file (.step/.iges/.stl/.obj/…) is IMPORTED as a fresh part doc instead (so manual
// File>Open/Import of a neutral file works). `as_solid` promotes a closed mesh to a solid
// on import. Throws OpError("io_error") on a bad file.
nlohmann::json doc_open(const std::string& path, bool as_solid = false);

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

// T-W2 sketch completion suite.
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

// sketch_project_ref: read-only projection of a datum plane / planar face / model edge
// into the sketch's (u,v) plane (engine/doc/session.py sketch_project_ref). `ref` is
// {"datum":...} or {"kind":"face|edge","selector":{...}}; returns {entity, source}.
// face/edge selectors resolve against the LAST solid body (single-body parts; the
// oracle's multi-body best-match is deferred). Throws OpError/unsupported_ref.
nlohmann::json doc_sketch_project_ref(const std::string& doc_id, const std::string& sketch_fid,
                                      const nlohmann::json& ref);

// sketch_project_entities (T-BL43 "Convert Entities" / external references): project a LIST of
// external refs (datum plane / face / model edge) onto the active sketch's (u,v) plane and
// return ready-to-insert doc-schema §3.1 REFERENCE entities (construction:true). Uses OCCT
// orthographic projection (GeomProjLib) + recon.cpp's curve classifier, so faces project all
// their boundary wires and tilted circles become ellipses correctly (the Convert-Entities fix).
// READ-ONLY (compiles current bodies, no mutation); the caller inserts the returned entities
// via sketch_add_entity ops so the projected geometry becomes constrainable. One-shot (not
// associative — see T-BL43 follow-up). `refs` is a list of {"datum":...} or
// {"kind":"face|edge","selector":{...}}. Returns {ok, entities, approx, sources, plane_frame}.
nlohmann::json doc_sketch_project_entities(const std::string& doc_id,
                                           const std::string& sketch_fid,
                                           const nlohmann::json& refs);

}  // namespace aicad::doc
