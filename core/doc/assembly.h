// Assembly compile + mate-solve + tessellation — C++ port of engine/doc/assembly.py.
//
// An assembly document (``aicad.asm/1``) is rebuilt very differently from a part:
//
//   1. Component loading — each non-suppressed component's ``source.path`` is opened
//      (parts cached per path+mtime). Native child assemblies resolve recursively,
//      preserving their local placements (provisional if unsolved); their descendant geometry is not
//      cached by the parent file's mtime. Bare neutral/mesh files use formats.cpp.
//      The resulting local-frame solids and topology describe mate references.
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
// motion-dynamics joint builder, T-FIN20) can anchor joint reactions at real mate
// geometry without re-running selector resolution.
struct AsmMateRefGeom {
    int ref_index = 0;                       // 0 or 1 (mate refs order)
    std::string component;                   // owning component id
    chudware::mate::MateGeom geom;              // local-frame plane/axis/point/sphere
};

// The result of compiling an assembly (mirror of assembly.py::AssemblyResult).
struct AssemblyResult {
    std::vector<AsmBodyOut> bodies;
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
    // mate_id -> its resolved refs (only refs whose selector resolved; T-FIN20 dynamics)
    std::map<std::string, std::vector<AsmMateRefGeom>> mate_geometry;
};

// Compile an assembly doc: load components, resolve mate refs, solve mates, place bodies.
// `base_dir` resolves relative source paths; `warm` (optional) speeds + stabilises the
// solve across re-solves. `drag` (optional) threads a soft-target through the solver.
AssemblyResult compile_assembly(const nlohmann::json& doc,
                                const std::string& base_dir = std::string(),
                                const std::optional<chudware::mate::WarmMap>& warm = std::nullopt,
                                const std::optional<chudware::mate::Drag>& drag = std::nullopt);

// Fingerprints of every non-suppressed component's resolved source file, followed by
// nested descendants ([{path, size, mtime}] — the same file identity as the load cache).
// Written into persisted-geometry cache headers so doc_open detects child appearance
// and geometry changes even when the containing assembly file has not changed.
// Missing files fingerprint as size/mtime 0; repeated/cyclic sources are traversed once.
nlohmann::json asm_component_fingerprints(const nlohmann::json& doc,
                                          const std::string& base_dir);

// True iff asm_component_fingerprints(doc, base_dir) == fp (order-sensitive — component
// order is part of the identity). Any parse mismatch returns false (cache miss).
bool asm_fingerprints_match(const nlohmann::json& doc, const std::string& base_dir,
                            const nlohmann::json& fp);

// Per-component explode offset vector (mm) from the doc's ``explode`` steps, scaled by
// factor (port of assembly.py::explode_offsets).
std::map<std::string, Eigen::Vector3d> explode_offsets(const nlohmann::json& doc, double factor);

// World-place a component body: apply the solved 16-float transform (+ optional explode
// offset) to its local shape. Port of assembly.py::placed_world_shape.
TopoDS_Shape placed_world_shape(const AsmBodyOut& body,
                                const std::optional<Eigen::Vector3d>& explode_offset = std::nullopt);

// The gp_Trsf placed_world_shape applies (solved transform + optional explode offset).
// Exposed so the assembly tessellation path can transform a CACHED local-frame mesh by
// this instead of tessellating the world-placed shape per open/re-solve.
gp_Trsf placed_world_trsf(const AsmBodyOut& body,
                          const std::optional<Eigen::Vector3d>& explode_offset = std::nullopt);

}  // namespace chudware::doc
