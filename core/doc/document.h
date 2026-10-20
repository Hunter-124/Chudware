// Document model — a rebuildable feature tree (C++ port of the orchestration core
// of engine/doc/compiler.py::compile_document). This is the layer that makes the
// C++ core genuinely PARAMETRIC: it sequences the geometry ops we already built
// (sketch -> extrude/revolve -> boolean -> fillet/chamfer) into a part, and
// RE-RUNS the whole tree on a parameter edit, producing new geometry. That
// re-run-from-a-changed-parameter loop is the essence of parametric CAD.
//
// WHY a separate translation unit (not part of features/booleans/dress):
//   The geom builders are pure shape -> shape (or shape*shape -> shape) functions,
//   independently testable. This file is the ONLY place that knows about the
//   feature-tree CONTRACT: feature ordering, id-keyed references between features,
//   which intermediate bodies survive vs. get consumed, and the never-raise rebuild
//   discipline. Keeping it isolated mirrors the Python split (compiler.py orchestrates;
//   features/*.py build geometry) and keeps the kernel layer free of tree semantics.
//
// SIMPLIFIED REF MODEL vs. the Python (deliberate, documented):
//   engine/doc/compiler.py resolves feature refs through a full SELECTOR system
//   (selectors.py resolves picks against live B-rep topology — "the top face of
//   feature F", "the 4 vertical edges of body B"). For THIS first increment the
//   refs are DELIBERATELY SIMPLIFIED to direct prior-feature-id strings:
//     extrude.profile = "<sketch-feature-id>"
//     boolean.a / boolean.b = "<solid-feature-id>"
//     fillet.body / chamfer.body = "<solid-feature-id>"
//   The full selector port is a later increment. The feature-dict SHAPE
//   (id / type / typed params) is kept aligned with the Python schema so doc-level
//   differential testing stays feasible once selectors land — only the ref payload
//   differs (id string here vs. selector object there).
//
// NEVER-RAISES CONTRACT (mirrors compile_document):
//   A feature that fails — bad ref, non-convergent sketch, empty boolean, an OCCT
//   throw — becomes an error FeatureStatus and the rebuild CONTINUES with the bodies
//   produced so far. The whole compile never throws across this API boundary.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>
#include <TopoDS_Shape.hxx>

#include "geom/tessellate.h"  // geom::DisplayQuality (display tessellation tier)

namespace chudware::doc {

// One feature's compile outcome, in tree order. `ok==false` carries a non-empty
// `error` (mirrors compiler.py's per-feature status dicts).
struct FeatureStatus {
    std::string id;
    std::string type;
    bool ok = false;
    std::string error;    // empty when ok
    std::string warning;  // non-fatal note on an ok feature (e.g. fillet rounded a subset of edges)
    double ms = 0.0;      // wall-clock this feature took to compile (rebuild profiling; 0 if untimed)
    // True when this feature's result was REUSED from the incremental-rebuild prefix cache
    // instead of recompiled (see CompileCache below). `ms` then reports the ORIGINAL compile
    // cost from the run that produced it, not this run's (near-zero) restore cost.
    bool reused = false;
    // Structured per-feature RESULT record, surfaced as `feature_status[].data`. Null for
    // every feature that has nothing measured to report; `heal` uses it to carry its
    // before/after BRepCheck verdict + volumes, which is the evidence that makes a repair
    // auditable instead of a claim.
    nlohmann::json data = nullptr;
};

// Per-surviving-body geometry stats (in AICM body order). Additive surfacing gate
// signal: open SURFACE bodies report meaningful AREA + face/edge counts (volume ~= 0),
// SOLID bodies report meaningful VOLUME. Mirrors the Python oracle's per-BodyOut props.
struct BodyInfo {
    std::string name;     // owning feature id (= AICM body name)
    std::string kind;     // "solid" | "surface"
    double volume = 0.0;  // enclosed volume (mm^3); ~0 for an open surface
    double area = 0.0;    // surface area (mm^2)
    int n_faces = 0;
    int n_edges = 0;
};

// One feature's pre-boolean TOOL SOLID (the prism/tool/tool_union/pattern union it
// applied to the running body, or the whole created solid for a base/standalone
// feature) + the boolean role it played. Populated additively at every boolean
// callsite in compile_document (extrude/revolve/sweep/rib/boolean/patterns/mirror/
// hole/bolt_circle/primitive) — the seam WS-B's doc_eval_feature_overlap needs to
// test feature-vs-feature overlap WITHIN one part (two bosses sharing volume, a
// cut that swallows an earlier boss, redundant overlapping cuts). Independent of
// tool_body_of/feature_op_of below (those store NESTING-oriented shapes tuned for
// pattern/mirror replay — e.g. an additive pattern's tool_body_of is its whole grown
// body — whereas a ToolRec always holds the actual incremental geometry added or
// removed, so a pairwise BRepAlgoAPI_Common between two ToolRecs measures real
// overlap, not "the whole body overlaps itself"). A suppressed or failed feature
// records nothing (the per-feature try/catch never reaches the capture call).
struct ToolRec {
    std::string feature_id;
    std::string role;   // "add" | "cut"
    TopoDS_Shape tool;  // the pre-boolean tool solid (refcounted handle, cheap to copy)
};

// The result of compiling a whole document.
struct CompileResult {
    std::vector<FeatureStatus> statuses;  // one per feature, in tree order
    std::vector<uint8_t> aicm;            // serialized SURVIVING solid bodies (write_aicm)
    int body_count = 0;                   // surviving solids after the whole tree
    double volume = 0.0;                  // total solid volume of surviving bodies (mm^3)
    std::vector<BodyInfo> bodies_info;    // per-surviving-body stats (surfacing parity gate)
    // Per-creating-feature topology map (host picking): {fid: {bodies:[ordinal...],
    // faces:[[ordinal,topo_id]...], edges:[...]}} with 1-based AICM body ordinal +
    // 1-based TopExp topo index. Empty for an all-error compile. A face/edge belongs to the
    // feature that GENERATED it; one a later feature only MODIFIED (trimmed by a chamfer,
    // split by a boss, merged by UnifySameDomain) keeps its earlier owner (geom/op_history.h).
    nlohmann::json owner_map;
    // The surviving solid shapes in AICM body order (shapes[i] <-> body ordinal i+1).
    // Lets a caller describe a picked body's topology (doc_resolve_pick).
    std::vector<TopoDS_Shape> shapes;
    // Owner (solid id: a feature id, or "<fid>#k" for a split piece) per 1-based TopExp
    // face/edge index of shapes[i]. Filled only by a `with_owners` compile (the
    // geometry-only component path, which has no owner_map) — assembly pick resolution.
    std::vector<std::map<int, std::string>> shape_face_owners, shape_edge_owners;
    // Per-feature pre-boolean tool solids + add/cut role (see ToolRec). Empty for a
    // doc with no boolean-producing features, or when every feature errored.
    std::vector<ToolRec> tool_recs;
    // Per-weld_member-feature cut-list side-channel: {fid: {profile, dims,
    // members:[{segment_id, profile, length, start[3], end[3], area, volume}]}}.
    // Surfaced from the weld_member compiler so the weldment cut list groups its
    // members without re-deriving geometry (port of compiler.py's _weld_members ->
    // CompileResult.weld_members). Empty when the doc has no weld_member feature.
    nlohmann::json weld_members = nlohmann::json::object();
    // Per-sm_flat_pattern-feature flat-blank side-channel: {fid: {n_base:[3],
    // bend_lines:[[[x,y,z],[x,y,z]],...]}}. The flat SOLID is the surviving body; what
    // cannot be recovered from it is the base-plane normal + the FLAT-SPACE bend seam
    // lines, so the flatten compiler surfaces them here for export_flat_dxf (the 2D
    // outline + holes are read back off the solid's broad face). Empty when the doc has
    // no flat pattern. Parity basis: engine/doc/compiler.py CompileResult.sheet_metal_flat.
    nlohmann::json sheet_metal_flat = nlohmann::json::object();
    // Displayable reference geometry: datum axis/plane/point/csys features produce no body, so they
    // never reach the AICM. Each entry surfaces the resolved geometry so the host can DRAW them as
    // reference overlays (the host had no way to show a created datum before). Shapes:
    //   axis  -> {id,name,kind:"axis",  point:[3], dir:[3]}
    //   plane -> {id,name,kind:"plane", origin:[3], x_dir:[3], y_dir:[3], normal:[3]}
    //   csys  -> same as plane with kind:"csys"
    //   point -> {id,name,kind:"point", point:[3]}
    // Empty for a doc with no datum features. (Parity: engine/doc/session.py display_datums.)
    nlohmann::json datums = nlohmann::json::array();
    bool ok = false;                      // true iff every feature compiled ok
    // The active configuration whose overrides/suppressions this compile reflects
    // ("Default" for a config-less doc). Surfaced in the ops-response doc_status so
    // the host's config switcher knows which config the geometry matches.
    std::string active_configuration = "Default";
    // Incremental-rebuild accounting (CompileCache): how many features were served from a
    // cached prefix checkpoint vs actually executed this compile. reused+executed may be
    // less than statuses.size() only never — they always sum to the feature count. 0/N on
    // a cache-less compile. Surfaced in ops_response.rebuild.doc_status for tests/profiling.
    int features_reused = 0;
    int features_executed = 0;
    // Phase timing (ms): the feature loop (incl. restored-prefix bookkeeping), the
    // post-loop tessellation/AICM build, and the owner-map topology diff. On a
    // prefix-reuse edit the loop shrinks toward zero and these expose what remains —
    // the profiler's "where does an incremental edit actually spend time" answer.
    double loop_ms = 0.0;
    double tess_ms = 0.0;
    double owner_ms = 0.0;
    // Tessellation phase broken down: BRepMesh meshing vs AicmBody extraction
    // (tess_ms − mesh − extract ≈ write_aicm serialization). Pinpoints whether an
    // incremental edit's tessellation cost is real re-meshing or re-extraction of
    // unchanged faces. From geom::tess_timing_read().
    double tess_mesh_ms = 0.0;
    double tess_extract_ms = 0.0;
    double tess_write_ms = 0.0;  // write_aicm serialization of all bodies
};

// Incremental-rebuild prefix cache (opaque; defined in document.cpp — it snapshots the
// compiler's internal per-feature state, whose types are private to that TU). One cache
// belongs to ONE document session and must only be used under that session's lock (the
// engine serialises all doc access through g_mtx today, so this holds by construction).
//
// How it works: every compile hashes a CHAIN over its inputs — h0 = fnv(env, active
// config), h[i+1] = fnv(h[i], feature[i].json) — and checkpoints the loop-carried
// compiler state after each executed feature. The next compile walks the new chain
// against the cached one, restores the DEEPEST matching checkpoint, and executes only
// the divergent suffix: editing feature k of N recompiles k..N, not 1..N. A fully
// matching chain (plus unchanged appearances) returns the cached CompileResult outright.
// Content-addressed, so undo/redo/config switches/failed-compile rollbacks can never be
// served stale state — a mismatch is just a cache miss. Checkpoint shapes are refcounted
// TopoDS handles shared with the live result, so memory cost is the intermediate
// (consumed) bodies of one compile, not N copies of the model.
struct CompileCache;
std::shared_ptr<CompileCache> make_compile_cache();
// Copy the mutable cache index; frozen checkpoints/results remain shared just as
// they do across incremental compiles. Caller holds the source session's lock.
std::shared_ptr<CompileCache> copy_compile_cache(const CompileCache& source);
// Global compile-cache counters for the profiler: {full_hits, features_reused,
// features_executed, compiles} since engine start (process-wide, all docs).
nlohmann::json compile_cache_stats();

// --- persisted build state (the `.step+` `cache.ckpt` entry; contracts/stepplus.md §4) ---
// Serialize the per-feature checkpoints of `cache` that still match `doc`'s compile chain
// (env + active config + `quality` + feature json), so a reopened document restores them
// and its first edit executes only the divergent tail instead of the whole tree. Every
// shape of every checkpoint is written into ONE BinTools set (sub-shape and geometry
// sharing survive the round trip, so TopExp ordinals and owner attribution are
// unchanged); results/statuses/owner maps are pooled so shared slots are written once.
// Only checkpoints after a run of successful features are kept (the prefix before the
// first failed feature); the full result is included when every feature succeeded and
// the cache's identical-inputs entry is current. Larger than `budget_bytes` -> the
// OLDEST checkpoints are dropped (halving the kept span each step, deterministic), then
// the full result, until it fits. Returns "" when nothing is written. `report` always
// receives {written, bytes, checkpoints_total, checkpoints_written, checkpoints_dropped,
// full_result, reason?} — `reason` names why something was dropped or not written.
std::string compile_cache_encode(const CompileCache& cache, const nlohmann::json& doc,
                                 const nlohmann::json& env, ::geom::DisplayQuality quality,
                                 const std::string& engine_key, const std::string& doc_hash,
                                 std::size_t budget_bytes, nlohmann::json& report);
// Restore a `cache.ckpt` blob into a fresh cache for `doc` compiled at `quality`.
// Validity is content-addressed per checkpoint: only the prefix whose stored chain
// equals `doc`'s chain (recomputed at the saved display quality) is restored, and it is
// re-keyed to `quality` (checkpoints hold exact B-rep without triangulation, so a tier
// change only re-meshes). The saved full result is restored only for an identical
// chain + appearances at the same quality AND when `aicm` (the container's cache.aicm,
// may be null) is byte-identical to the mesh it was built with. Returns nullptr when
// nothing is usable; `report` receives {restored, checkpoints, matched_features, features,
// full_result, full_result_reason?, reason?}; reason/full_result_reason name the miss:
// engine_key, decode_error, chain_mismatch, not_saved, display_quality, appearance_changed,
// mesh_mismatch. Never throws.
std::shared_ptr<CompileCache> compile_cache_decode(const std::string& blob,
                                                   const nlohmann::json& doc,
                                                   const nlohmann::json& env,
                                                   ::geom::DisplayQuality quality,
                                                   const std::string& engine_key,
                                                   const std::string* aicm,
                                                   nlohmann::json& report);
// The identical-inputs result held by `cache` (nullptr when none).
std::shared_ptr<const CompileResult> compile_cache_full_result(const CompileCache& cache);
// A process-unique stamp that changes whenever the cache's contents change (commit or
// restore); copies share it. Lets a saver reuse an encoding of unchanged contents.
uint64_t compile_cache_stamp(const CompileCache& cache);

// Display tessellation of already-solved bodies without executing any feature: meshes
// r.shapes at `quality` with the doc's body/face appearances (same tiers, colors and AICM
// layout as compile_document's surviving-body pass) and refreshes r.aicm, r.bodies_info
// stats (names/kinds must already be set) and r.volume. With `tessellate=false` only the
// per-body stats are recomputed (r.aicm untouched) from the exact geometry.
void tessellate_solved_bodies(const nlohmann::json& doc, CompileResult& r,
                              ::geom::DisplayQuality quality, bool tessellate = true);

// Shared configuration interpretation for compilation and dependency inspection.
// The resolved configuration pointer borrows from doc; null means implicit Default.
std::pair<std::string, const nlohmann::json*> resolve_active_config(const nlohmann::json& doc);
nlohmann::json config_effective_features(const nlohmann::json& features,
                                         const nlohmann::json* config);

// Compile a document into geometry. `doc` = {"features":[ ... ]}. `env` maps
// equation variable names to numbers (for expression-valued params / dims).
//
// Never throws: a feature that fails gets an error status and the rebuild
// continues with the bodies produced so far (compiler.py contract). Call it twice
// with the SAME `doc` and DIFFERENT `env` to re-run the tree into new geometry —
// that is the parametric-rebuild payoff.
//
// `cache` (optional): the session's incremental-rebuild cache. When supplied, an
// unchanged feature PREFIX is restored from its checkpoint instead of recompiled
// (see CompileCache above); geometry is bit-identical to a full rebuild because the
// checkpoint holds the actual shapes the previous compile produced. nullptr = the
// previous always-full-rebuild behavior, byte-for-byte.
// `quality` (display tessellation): selects the BRepMesh linear/angular deflections the
// AICM display bodies are meshed at (geom::display_deflections). Defaults to Fine — the
// smooth-display tier — so a caller that does not care gets the best-looking mesh.
// `with_display=false` is for component geometry consumers: retain exact per-body
// triangulations at `quality`, shapes/statuses/count, but omit AICM extraction and
// serialization, body statistics, picking owner-map inversion and datum display data.
// `with_owners=true` additionally attributes face/edge owners and returns them per shape
// (shape_face_owners/shape_edge_owners) — a component's owning feature for an assembly pick.
CompileResult compile_document(const nlohmann::json& doc, const nlohmann::json& env,
                               CompileCache* cache = nullptr,
                               ::geom::DisplayQuality quality =
                                   ::geom::DisplayQuality::Fine,
                               bool with_display = true, bool with_owners = false);

// Per-body appearance (C26 colour + C32-A RealView material) for AICM body ordinal `body_num`
// (1-based). Reads doc["appearances"]["b<body_num>"] — a bare [r,g,b] (colour only) or
// {color, metallic, roughness} — and overwrites color/metallic/roughness in place when present
// (values clamped to 0..1). No appearances entry => the defaults are left untouched. This is what
// bakes a set_appearance op into the rendered/exported geometry; shared by the compile AICM build
// and the apply-ops tess metadata so both report the same colour.
void apply_doc_appearance(const nlohmann::json& doc, uint32_t body_num,
                          float color[3], float& metallic, float& roughness);

// Physical material registry key of part body `body_num` (1-based): the
// doc["body_materials"]["b<body_num>"] override, else the document material, else "".
std::string doc_body_material_key(const nlohmann::json& doc, uint32_t body_num);

// Exact SID inventory paired with actual TopExp::MapShapes 1-based face IDs. A SID
// may have multiple candidates (quantized hash collision); never choose one by ordinal.
using FaceSidInventory = std::map<std::string, std::vector<std::pair<uint32_t, nlohmann::json>>>;
FaceSidInventory face_sid_inventory(const TopoDS_Shape& shape);
// Persisted face_appearances[bN][record_id] = {selector, appearance}; record ids are opaque.
// Only exact, uniquely matching fingerprints paint faces. Stale/ambiguous records stay inert.
void apply_doc_face_appearances(const nlohmann::json& doc, uint32_t body_num,
                                const TopoDS_Shape& shape, aicm::AicmBody& body);

// One push-pull plan: which feature/param a dragged face drives + the new value
// (port of engine/doc/instant3d.py plan_edit). `draggable==false` carries a `reason`.
struct PushPullPlan {
    bool draggable = false;
    std::string reason;       // why not draggable (when draggable==false)
    std::string owner_fid;    // owning feature id ("" if the face has no owner)
    std::string ftype;        // owning feature type
    std::string param;        // drivable param key (when draggable)
    double old_value = 0.0;
    double new_value = 0.0;
    bool clamped = false;     // new_value was limited to the 0.1mm floor
    std::string axis;         // axis label x/y/z/radial/normal/n/axial ("" = none)
    bool has_axis = false;    // false -> emit axis as JSON null
};

// Plan a push-pull drag of face (1-based body_id, 1-based topo_id) by `distance` mm.
// Compiles `doc`, resolves the picked face's owning feature from the owner map, and
// dispatches on the face surface + feature type to the single drivable length/radius/
// offset param. NEVER mutates the doc. Throws OpError("invalid_op") for an out-of-range
// body_id/topo_id or an assembly (mirrors instant3d.py _resolve_face_owner); otherwise
// returns a plan whose `draggable` flag tells the caller whether to apply it.
PushPullPlan plan_push_pull(const nlohmann::json& doc, const nlohmann::json& env,
                            int body_id, int topo_id, double distance);

// Resolve a host PICK into a re-resolvable selector: compile `doc`, take the surviving
// body at 1-based `body_id`, and describe its `kind` ("face"/"edge"/"vertex") sub-shape
// at 1-based `topo_id` (TopExp::MapShapes numbering = the host's pick ids), tagging the
// owning feature from the owner map. Returns the selector json, or null if out of range.
// Port of engine/doc/session.py::resolve_pick.
nlohmann::json resolve_pick(const nlohmann::json& doc, const nlohmann::json& env,
                            int body_id, const std::string& kind, int topo_id);
// Overload over an already-compiled document (an open session's cached CompileResult) so a pick
// resolves without recompiling the whole feature tree on every click.
nlohmann::json resolve_pick(const CompileResult& r, int body_id, const std::string& kind,
                            int topo_id);

// The unique edge topo_ids bordering a face — for "fillet a whole face" (host expands a face pick
// into its edges). Empty on a bad id.
std::vector<int> face_edges(const nlohmann::json& doc, const nlohmann::json& env,
                            int body_id, int face_topo_id);
std::vector<int> face_edges(const CompileResult& r, int body_id, int face_topo_id);

}  // namespace chudware::doc
