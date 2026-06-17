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
#include <string>
#include <vector>

#include <nlohmann/json.hpp>
#include <TopoDS_Shape.hxx>

namespace aicad::doc {

// One feature's compile outcome, in tree order. `ok==false` carries a non-empty
// `error` (mirrors compiler.py's per-feature status dicts).
struct FeatureStatus {
    std::string id;
    std::string type;
    bool ok = false;
    std::string error;  // empty when ok
};

// The result of compiling a whole document.
struct CompileResult {
    std::vector<FeatureStatus> statuses;  // one per feature, in tree order
    std::vector<uint8_t> aicm;            // serialized SURVIVING solid bodies (write_aicm)
    int body_count = 0;                   // surviving solids after the whole tree
    double volume = 0.0;                  // total solid volume of surviving bodies (mm^3)
    // Per-creating-feature topology map (host picking): {fid: {bodies:[ordinal...],
    // faces:[[ordinal,topo_id]...], edges:[...]}} with 1-based AICM body ordinal +
    // 1-based TopExp topo index. Empty for an all-error compile. (Linear single-chain
    // attribution; matches engine/doc/session.py::_owner_map for the common case.)
    nlohmann::json owner_map;
    // The surviving solid shapes in AICM body order (shapes[i] <-> body ordinal i+1).
    // Lets a caller describe a picked body's topology (doc_resolve_pick).
    std::vector<TopoDS_Shape> shapes;
    bool ok = false;                      // true iff every feature compiled ok
};

// Compile a document into geometry. `doc` = {"features":[ ... ]}. `env` maps
// equation variable names to numbers (for expression-valued params / dims).
//
// Never throws: a feature that fails gets an error status and the rebuild
// continues with the bodies produced so far (compiler.py contract). Call it twice
// with the SAME `doc` and DIFFERENT `env` to re-run the tree into new geometry —
// that is the parametric-rebuild payoff.
CompileResult compile_document(const nlohmann::json& doc, const nlohmann::json& env);

// Resolve a host PICK into a re-resolvable selector: compile `doc`, take the surviving
// body at 1-based `body_id`, and describe its `kind` ("face"/"edge"/"vertex") sub-shape
// at 1-based `topo_id` (TopExp::MapShapes numbering = the host's pick ids), tagging the
// owning feature from the owner map. Returns the selector json, or null if out of range.
// Port of engine/doc/session.py::resolve_pick.
nlohmann::json resolve_pick(const nlohmann::json& doc, const nlohmann::json& env,
                            int body_id, const std::string& kind, int topo_id);

}  // namespace aicad::doc
