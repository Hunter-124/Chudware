# Contract: CAD Project Graph — v1

Append-only. Integrator-owned. Producer: `services/plm/cad_graph.py` (+ `engine_oracle/parts_hub/cad_graph_rpc.py`).
Consumers: PLM workspace graph tab (GUI), copilot traversal tools (MCP), documentation helpers.

Graphify/Sourcegraph analogue over CAD projects: how parts/features/mates/BOM/docs link.

- Nodes: `{id, kind, label, path?, doc_id?, meta{}}`; kinds:
  `project|doc|part|assembly|body|feature|sketch|mate|bom_item|drawing|sim_study|board|firmware`.
- Edges: `{src, dst, kind, meta{}}`; kinds:
  `CONTAINS|DEPENDS_ON|MATES|INSTANCE_OF|REFERENCES|DERIVES_FROM|DOCUMENTS|SIMULATES|MANUFACTURES`.
- Provenance: every node/edge carries `origin: extracted|inferred` (extracted = deterministic
  from doc data; inferred = heuristic/AI). GUI must visually distinguish.
- Worker RPCs:
  - `cad_graph_get {project_dir?|doc_id?} -> {ok, nodes, edges, generated_at}`
  - `cad_graph_blast {node_id} -> {ok, affected:[node_id], edges}` (transitive dependents)
  - `cad_graph_path {src, dst} -> {ok, path:[node_id]}`
- Doc links: shared-dimension/layout links between parts are first-class REFERENCES edges with
  `meta:{dim, value, unit}` so assembly-fit documentation is queryable.
- Storage: derived cache under project `.plm/graph.json`; rebuilt incrementally on doc save /
  AI-VC checkpoint; never authoritative (recompute always allowed).

## Addenda (append-only, additive to v1)

### Equation nodes + expression-driven dimensional links
- New node kind `equation` — one per `equations.globals` entry (contracts/doc_schema.md §2/§4):
  `id="eq:{doc_node_id}:{name}"`, `label=name`, `meta:{expr, value, unit}`; `doc CONTAINS eq`.
- A feature param / sketch dimension whose dimension value carries a non-null `expr`
  (`{value, expr}`, doc_schema §0) emits `feature DERIVES_FROM eq` for each referenced global,
  `meta:{dim, expr, value, unit}`, `origin=extracted`. An equation whose own `expr` references
  another global emits `eq DERIVES_FROM eq` (`meta:{expr, via:"equation"}`).
- Cross-doc `feature.derived_from {doc_id|doc_name, feature}` resolves to `feature DERIVES_FROM
  <doc>` (`meta:{feature, via:"derived_from"}`); unresolvable refs are dropped.
- Sketch `params.external_refs` (doc_schema §3.5/§3B) emit `feature REFERENCES <owner>`
  (`meta:{via:"external_ref", stale}`); same-doc owner or `__resolve__` cross-doc.
- Mate nodes and their `MATES` edges carry `meta:{value, unit, expr}` (the dimension of a
  distance/angle mate); a mate `expr` referencing a global emits `mate DERIVES_FROM eq`.
- `MANUFACTURES`: a CAM doc (`schema` base `aicad.cam`) emits `doc MANUFACTURES <source>` over
  keys `source|source_doc|part|stock` (`stock.from_solid` for a dict stock).

### Origins
- Edge/node `origin` vocabulary extends to `extracted|inferred|manual|ai`.
- The shared name+value dimension match (heuristic) is now `origin=inferred`,
  `meta:{dim, value, unit, via:"shared_value"}` — expression-driven `DERIVES_FROM` edges are the
  extracted truth.

### Manual / AI-authored link overlay
- Store `<project>/.plm/graph_links.json`, schema `aicad.cad_graph_links/1`:
  `{schema, links:[{id:"l<8hex>", src, dst, kind, note, origin:"manual"|"ai", author{}, created_at}]}`.
  Missing file = empty overlay (zero-config). `build_graph` merges each link whose `src`/`dst`
  node ids exist as an edge with its `origin` and `meta:{note, link_id}`.
- Worker RPCs (invalidate the graph cache on success):
  - `cad_graph_link_add {project_dir, src, dst, kind, note?, origin?="manual"} -> {ok, link}`
    (`invalid_kind` when `kind ∉` edge kinds; `invalid_link` when src==dst or empty).
  - `cad_graph_link_remove {project_dir, link_id} -> {ok, removed}`.
  - `cad_graph_link_list {project_dir} -> {ok, links}`.

### Assembly best-practice advisor
- `assembly_advice(graph) -> {ok, suggestions:[{kind, components, reason}], narrative}`.
  `kind ∈ extract_subassembly | group_into_subassemblies | split_multibody`. Deterministic over
  MATES clustering (≥3 components / ≥2 internal mates), flat-assembly (>12 direct components), and
  multi-body (>2 bodies) thresholds. Surfaced via the MCP `assembly_plan_review` tool.
