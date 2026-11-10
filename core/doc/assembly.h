// Assembly compile + mate-solve + tessellation — C++ port of engine/doc/assembly.py.
//
// An assembly document (``aicad.asm/1``) is rebuilt very differently from a part:
//
//   1. Component loading — each non-suppressed component's ``source.path`` resolves
//      to a live committed snapshot or disk, cached by native document hash or file
//      stamp and recursive dependency identity. Native child assemblies resolve
//      recursively and retain local placements (provisional if unsolved). Bare
//      neutral/mesh files use formats.cpp. Local
//      topology descriptions are computed only when mate references require them.
//      Embedded-only sources are unsupported and produce explicit component errors.
//
//   2. Mate-ref resolution — every mate ref {component, selector} resolves the selector
//      against that component's LOCAL geometry (doc/selectors.{h,cpp}) into a
//      chudware::mate::MateGeom (plane/axis/point/sphere) in the component's local frame.
//
//   3. Mate solve — chudware::mate::solve_mates over the shared numeric core places every
//      floating component. A fixed component is pinned as a hard constraint: its authored
//      transform is what the mates are solved against and what comes back, bit-identical,
//      so session.cpp's write-back of the solved poses cannot re-pose the anchor. Solved
//      transforms + a per-mate status list + the DOF summary are reported.
//
//   4. Tessellation — each component contributes ONE AICM body (body_id = ordinal,
//      name = component name), world-placed by the solved transform (+ an optional
//      explode offset). Face appearance regions retain authored child-body finishes.
//
// Canonical units: mm / degrees / grams.
#pragma once

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <Eigen/Dense>
#include <nlohmann/json.hpp>
#include <TopoDS_Shape.hxx>
#include <gp_Trsf.hxx>

#include "mate/mate.h"
#include "geom/aicm.h"
#include "doc/document.h"

namespace chudware::doc {

// One placed component body in the assembly result.
struct AsmBodyOut {
    int body_id = 0;
    std::string component_id;
    std::string name;
    TopoDS_Shape shape_local;            // local-frame compound of the component's bodies
    std::vector<double> transform;       // solved 16-float row-major (world placement, no explode)
    std::array<float, 3> color{0.62f, 0.70f, 0.82f};  // part-renderer neutral fallback
    float metallic = 0.0f;
    float roughness = 0.5f;
    std::vector<aicm::AppearanceRegion> appearance_regions;  // local compound face IDs
    // Material registry key per source body, aligned with shape_local's bodies (the shape
    // itself for one body, else its compound children); "" = no material assigned.
    std::vector<std::string> body_material_keys;
};

// Per-component build status. ok requires the ENTIRE source to rebuild successfully,
// including descendant features/components and nested mate placement. A failed status
// may still contribute surviving display geometry; error describes every source failure.
struct AsmComponentStatus {
    std::string id;
    bool ok = false;
    double ms = 0.0;
    std::optional<std::string> error;        // nullopt == ok
    std::vector<std::string> dangling_refs;  // refs[i] strings (none here; mate-level)
    // Write-back report of a component this rebuild compiled from its features
    // (compile_component_with_build_state); null when its saved build state served.
    nlohmann::json build_state;
};

// Per-mate status (id, type, status, dangling_refs).
struct AsmMateStatus {
    std::string id;
    std::string type;
    std::string status;                      // ok|redundant|conflicting|suppressed|unresolved
    std::vector<std::string> dangling_refs;
    // Largest absolute entry of this mate's residual at the solved pose (mm for the
    // positional rows, dimensionless for the direction rows). 0 for a mate that
    // contributed no rows (suppressed / unresolved).
    double residual = 0.0;
    std::string reason;
};

// One RESOLVED mate reference: which component owns it + the local-frame geometry
// its selector resolved to. Exposed on AssemblyResult so downstream consumers (the
// motion-dynamics joint builder) can anchor joint reactions at real mate
// geometry without re-running selector resolution.
struct AsmMateRefGeom {
    int ref_index = 0;                       // 0 or 1 (mate refs order)
    std::string component;                   // owning component id
    chudware::mate::MateGeom geom;              // local-frame plane/axis/point/sphere
};

// The result of compiling an assembly (mirror of assembly.py::AssemblyResult).
struct AssemblyResult {
    std::vector<AsmBodyOut> bodies;
    // Retain the cached loads used by this live result, including topology and
    // source documents, even when other assemblies exceed the global LRU limit.
    std::vector<std::shared_ptr<const void>> component_cache_pins;
    std::vector<AsmComponentStatus> component_status;
    std::vector<AsmMateStatus> mate_status;
    std::vector<FeatureStatus> feature_status;  // authored assembly patterns, not components/mates
    int dof = 0;
    bool over_defined = false;
    bool converged = true;                  // local mate solve AND complete, solved child sources
    double max_residual = 0.0;               // worst residual over every active mate
    double tolerance = 0.0;                  // the size-scaled threshold `converged` used
    std::string worst_mate;                  // mate id owning max_residual ("" if none)
    std::string dof_summary;
    std::map<std::string, std::vector<double>> transforms;  // component_id -> 16f (solved)
    chudware::mate::WarmMap warm;                               // component_id -> (t,w)
    // mate_id -> its resolved refs (only refs whose selector resolved; used by dynamics)
    std::map<std::string, std::vector<AsmMateRefGeom>> mate_geometry;
};

// Immutable committed state of a path-backed open document. Assembly loads never
// acquire another document's mutex: sessions publish these snapshots on unlock.
struct AssemblyLiveSource {
    std::shared_ptr<const nlohmann::json> document;
    std::shared_ptr<const CompileResult> part;
    std::string doc_hash;
    std::string source_doc_id;
    long revision = -1;
    nlohmann::json base_file_stamp;
    bool unsaved = false;
    bool unverified = false;
};
std::shared_ptr<const AssemblyLiveSource> assembly_live_source(const std::string& canonical_path);
using AssemblyLiveSources = std::map<std::string, std::shared_ptr<const AssemblyLiveSource>>;
std::shared_ptr<const AssemblyLiveSources> assembly_live_sources();

// Freeze the source set for one complete assembly compile, including prefetch
// workers. Its fingerprint and geometry then always describe the same snapshots.
class AssemblySourceScope {
public:
    AssemblySourceScope();
    explicit AssemblySourceScope(std::shared_ptr<const AssemblyLiveSources> sources);
    ~AssemblySourceScope();
    AssemblySourceScope(const AssemblySourceScope&) = delete;
    AssemblySourceScope& operator=(const AssemblySourceScope&) = delete;
    static std::shared_ptr<const AssemblyLiveSources> current();
private:
    std::shared_ptr<const AssemblyLiveSources> previous_;
};

// In-memory source identities use live documents (including nested assemblies),
// while the persisted fingerprint API below always describes files on disk.
nlohmann::json asm_live_component_fingerprints(const nlohmann::json& doc,
                                               const std::string& base_dir);
bool asm_live_fingerprints_match(const nlohmann::json& doc, const std::string& base_dir,
                                 const nlohmann::json& fingerprints);

// Compile an assembly doc: load components, resolve mate refs, solve mates, place bodies.
// `base_dir` resolves relative source paths; `warm` (optional) speeds + stabilises the
// solve across re-solves. `drag` (optional) threads a soft-target through the solver.
AssemblyResult compile_assembly(const nlohmann::json& doc,
                                const std::string& base_dir = std::string(),
                                const std::optional<chudware::mate::WarmMap>& warm = std::nullopt,
                                const std::optional<chudware::mate::Drag>& drag = std::nullopt);

// Fingerprints of every non-suppressed component's resolved source file, followed by
// nested descendants and legacy file-backed imports ([{path, size, mtime, doc_hash?}];
// native .step+ rows carry their document's stepplus_doc_hash).
// Written into persisted-geometry cache headers so doc_open detects child appearance
// and geometry changes even when the containing assembly file has not changed.
// Missing files fingerprint as size/mtime 0; components without a file source retain
// an empty-path row. Repeated/cyclic sources are traversed once.
// Only source JSON is read; display meshes, BREP, studies and attachments are skipped.
nlohmann::json asm_component_fingerprints(const nlohmann::json& doc,
                                          const std::string& base_dir);

// True iff disk fingerprints match fp row for row (order-sensitive): same path,
// and matching document hashes when both rows carry one, else matching size+mtime.
// A build-state rewrite keeps the document hash. Parse mismatches are cache misses.
bool asm_fingerprints_match(const nlohmann::json& doc, const std::string& base_dir,
                            const nlohmann::json& fp);

// A component part whose saved build state is stale: compile it as a part session would
// and save that build into its file (contracts/stepplus.md §17), so the next assembly
// rebuild and a direct open of the part are both warm. `stamp` is the file's
// stepplus_file_stamp taken before `doc` was read. Implemented in session.cpp. Returns
// the compile (bodies for the assembly); `build_state` receives the write-back report.
struct NativeFileStamp;
// The part file's saved build checkpoints (cache.ckpt) decoded for `doc` at `quality`, or
// null when the file has none, is unverified or unreadable. Checkpoints are content-addressed
// per feature, so `doc` may be an edited live/recovered revision of that file: only its
// unchanged feature prefix is restored. Feed it to a display-mode compile_document.
std::shared_ptr<CompileCache> component_saved_checkpoints(const std::string& abspath,
                                                          const nlohmann::json& doc,
                                                          ::geom::DisplayQuality quality);
CompileResult compile_component_with_build_state(const std::string& path,
                                                 const nlohmann::json& doc,
                                                 const NativeFileStamp& stamp,
                                                 nlohmann::json& build_state);

// Per-component explode offset vector (mm) from the doc's ``explode`` steps, scaled by
// factor (port of assembly.py::explode_offsets).
std::map<std::string, Eigen::Vector3d> explode_offsets(const nlohmann::json& doc, double factor);

// Automatic explode vectors (mm, the factor-1 offset) for an assembly without authored
// ``explode`` steps. The largest component stays put; every other component leaves the
// smallest bigger component its box sits in (its parent) through the cheapest exit — its
// mate insertion axis when it has one, else a world axis cleared against the parent's
// face boxes — and rides along with that parent. Siblings stack so exploded parts never
// land inside each other. Components that stay put are omitted.
std::map<std::string, Eigen::Vector3d> auto_explode_vectors(const AssemblyResult& r);

// World-place a component body: apply the solved 16-float transform (+ optional explode
// offset) to its local shape. Port of assembly.py::placed_world_shape.
TopoDS_Shape placed_world_shape(const AsmBodyOut& body,
                                const std::optional<Eigen::Vector3d>& explode_offset = std::nullopt);

// The gp_Trsf placed_world_shape applies (solved transform + optional explode offset).
// Exposed so the assembly tessellation path can transform a CACHED local-frame mesh by
// this instead of tessellating the world-placed shape per open/re-solve.
gp_Trsf placed_world_trsf(const AsmBodyOut& body,
                          const std::optional<Eigen::Vector3d>& explode_offset = std::nullopt);

// The feature — an id in the picked component's OWN part document — that owns face/edge
// `topo_id` of `body.shape_local` (the TopExp numbering doc_resolve_pick uses). The part is
// compiled once with owner attribution (cached by live/disk source identity); the pick
// is matched to the compiled body by index AND verified by area/length + centroid, falling
// back to a unique geometric match. A pattern instance ("<cid>#k") resolves through its seed component.
// "" when the component is not a native part (neutral import, nested assembly), `kind` is not
// face|edge, or no confident match exists.
std::string component_pick_owner(const nlohmann::json& asm_doc, const std::string& base_dir,
                                 const AsmBodyOut& body, const std::string& kind, int topo_id);

}  // namespace chudware::doc
