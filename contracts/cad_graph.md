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
