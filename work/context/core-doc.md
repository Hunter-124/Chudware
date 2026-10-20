# core/doc — document, session, compile, reconstruction and RPC surface

Scope: core/doc/**, core/api/**
Contracts: contracts/doc_schema.md, doc_ops.md, worker_rpc.md, stepplus.md, mesh_format.md, selectors.md
Canonical guidance: .claude/skills/chudware-dev/SKILL.md (geometry history, document publishing, save/selection invariants).

## Entry points
- `core/doc/document.{h,cpp}` — document model and feature compilation.
- `core/doc/session.{h,cpp}` — stateful document RPCs: `doc_new`, `doc_apply_ops`,
  undo/redo, tree/rebuild, evaluation and render/view operations.
- `core/doc/recon.{h,cpp}` — OCCT reconstruction primitives, independent of Session;
  document-resolving wrappers belong in `session.cpp`.
- `core/doc/selectors.{h,cpp}`, `picks.{h,cpp}` — persistent selectors and
  `resolve_geom_query` geometry queries.
- `core/doc/stepplus.{h,cpp}` — native `.step+` persistence.
- `core/doc/formats.{h,cpp}`, `dxf.cpp`, `export2d.cpp`, `pdfvec.cpp` — import/export.
- `core/doc/drawing.cpp`, `drawing_session.cpp`, `annot_draw.cpp` — drawing documents.
- `core/doc/assembly.cpp`, `sketch_geom.cpp`, `validate.cpp` — assembly, sketch
  placement and validation.
- `core/api/libchudware.{h,cpp}`, `chudware_probe.cpp` — flat C ABI and RPC dispatch.

## How it connects
GUI jobs/DocMirror → `services/worker/worker_client.cpp` → hostd/libchudware →
document session → feature compiler (`core/sketch`, `core/geom`). MCP tools enter
through `services/mcp/core_bridge.py`. Ops return revision/tree/feature status,
ownership and tessellation for the host's one AICM loader.

## Debugging playbook
- Integrator build: `./build.sh core` (consumed trees; see build-test.md).
- Gate-tree tests: `ctest --test-dir core/build-linux --output-on-failure -R 'document|selectors|picks|sketch_on_face|sketch_drag_rpc|polygon|drawing_gdt|pattern|mirror|interference|hole'`.
  For a separately configured OCCT development tree, use `core/build-occt-linux`.
- `picks.cpp::resolve_geom_query` builds shape-local candidates without ownership;
  `session.cpp` joins document ownership for `doc_query`. Trace both before changing
  an owner selector; do not mistake the shape-only helper for the whole RPC.
- Render view normalization lives in `session.cpp::resolve_view_spec`; all render
  consumers must agree on its named refusals and explicit-camera semantics.
- Booleans use `geom::fuse_unify` / `cut_unify`; rebuilt topology carries operation
  history/provenance. Do not weaken regression assertions or fabricate parity results.
- Document publication and save/open operations must preserve the user's active tab,
  live session and editable feature history; canonical dev guidance owns the details.
- Dress-feature commit validates the final fillet/chamfer solid (B-rep, zero-area
  faces and non-destructive self-intersection check). Failure keeps its preceding
  body; changing this policy requires a STEP+ cache-engine epoch bump.
- Atomic `doc_apply_ops` failures return `rejected_tree` alongside native
  `feature_status` while the live tree/revision are restored. Diagnosis must use
  that candidate, not look for the new failed feature in the restored tree.
- Detached assembly cache validation hashes the persisted authored tree before
  session-local absolute-path/name rewrites; engine/display/dependency checks remain.
- Solid health checks SI in a proper rigid constituent-face frame, including nested
  import wrappers. All relative geometry and world-space evidence remain intact;
  scale/shear baked into geometry and genuinely folded skins are still checked.
- Export tessellation resolves tangent-cusp boundary chords on native curves and
  synchronizes located partner edges. STL checks actual serialized facet/edge
  topology; no proximity welding or generic mesh healing is a valid substitute.

## Graphify starters
- graphify query "doc_apply_ops document feature compiler"
- graphify explain "resolve_geom_query"
- graphify path "worker_client.cpp" "session.cpp"

## Related tasks/docs
- contracts/README.md — frozen seam ownership
- work/context/core-geom.md — geometry/sketch/solver layer
- work/context/worker-bridge.md — host routing and IPC
- docs/review/GEOMETRY_LLM_REPRESENTATION_REVIEW.md — geometry-description design
