# core/doc — document / session / compile, recon, RPC surface

Scope: core/doc/**, core/api/** (C ABI)
Contracts: contracts/doc_schema.md (aicad.part/1, aicad.asm/1, aicad.drawing/1), contracts/doc_ops.md (closed op vocabulary v1.1), contracts/worker_rpc.md (protocol 2), contracts/stepplus.md, contracts/mesh_format.md (AICM), contracts/selectors.md

## Entry points
- core/doc/document.h/.cpp — Document model (feature tree)
- core/doc/session.h/.cpp — DocSession, stateful RPC layer: doc_new/doc_apply_ops/doc_undo/doc_redo/doc_tree/doc_rebuild; the PRODUCTION-CUTOVER layer (C++ port of engine/doc/session.py); 42/42 stateful session parity
- core/doc/session.h — self-check RPCs: doc_eval_compare, doc_face_wires, doc_eval_interference, doc_eval_feature_overlap
- core/doc/recon.h/.cpp — reverse-engineering primitives (WS-C); pure OCCT, deliberately Session-free — doc-resolving glue lives in session.cpp wrappers
- core/doc/selectors.h/.cpp, core/doc/picks.h/.cpp — geometry selector resolution (resolve_geom_query)
- core/doc/stepplus.cpp/.h — native .step+ container read/write
- core/doc/formats.cpp/.h, dxf.cpp, export2d.cpp, pdfvec.cpp — format import/export
- core/doc/drawing.cpp, drawing_session.cpp, annot_draw.cpp — 2D drawing docs (aicad.drawing/1)
- core/doc/assembly.cpp, sketch_geom.cpp, validate.cpp — assemblies, sketch geometry, doc validation
- core/api/libaicad.cpp/.h, core/api/aicad_probe.cpp — C ABI exposing session.cpp to services/worker/

## How it connects
This is the heart of the C++ strangler: GUI/agent commands become doc_ops (contracts/doc_ops.md) sent
over newline-delimited JSON-RPC (contracts/worker_rpc.md) into DocSession, which compiles the feature
tree via core/geom and returns tree + AICM mesh. Downstream chain:
services/worker/worker_command_dispatcher.cpp → WorkerClient/DocMirror →
ui/abstract/command_dispatcher.h (ICommandDispatcher::dispatch) → ModelDiff (ui/abstract/variant.h).
Python oracle equivalent: engine/doc/session.py (frozen). contracts/ files above are append-only and
integrator-owned (AGENTS.md).

## Debugging playbook
- Build: `cmake -S core -B core/build-occt -DAICAD_WITH_OCCT=ON && cmake --build core/build-occt`
- Tests: `ctest --test-dir core/build-occt -R "document|selectors|picks|sketch_on_face|sketch_drag_rpc|polygon|drawing_gdt|pattern|mirror|interference|hole" --output-on-failure` (document is OCCT-gated, core/CMakeLists.txt ~L872)
- Gotcha: core/doc/picks.cpp resolve_geom_query hardcodes `ti.owner = std::nullopt` — ownership resolution is a known gap (REFACTOR_LOG L344)
- Gotcha: view-spec normalization must match core/doc/session.cpp::resolve_view_spec (REFACTOR_LOG L366)
- Gotcha: keep recon.{h,cpp} Session-free (mirrors geom/doc.h split); put doc-resolving glue in session.cpp (REFACTOR_LOG ~L425)
- Never weaken a parity assertion; match Python oracle unless Python is provably buggy (README)

## Graphify starters
- graphify query "how does doc_apply_ops flow from the worker RPC into DocSession and the feature compiler"
- graphify explain "DocSession"
- graphify query "how are selectors resolved to faces and edges"

## Related tasks/docs
- work/tasks: grep `recon|session|doc_apply_ops` in work/tasks/*.md (WS-C tasks T-C1..C5 cover recon/intake)
- contracts/README.md (seam ownership), docs/RECOGNITION_RESIDUAL.md (recon/recognition status)
- docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md (GSL describe_* built on this RPC surface)
