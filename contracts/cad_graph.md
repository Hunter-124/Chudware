# Contract: CAD Project Graph — v1

Append-only. Maintainer-owned. Producer: `services/plm/cad_graph.py` (+ `services/mcp/graph_tools.py`).
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

### Change probe + source fingerprint (2026-07)
- `cad_graph_get` full responses add `sources_mtime_max: float` and `sources_count: int` —
  a fingerprint of the source tree (newest `.step+`/`.ecad+`/overlay mtime + file count via
  `_source_mtimes_fast`).
- New optional request params `if_mtimes_max: float`, `if_sources_count: int`: when the
  on-disk cache is still fresh AND both fingerprint parts match, the response is the
  lightweight probe `{ok, unchanged: true, sources_mtime_max, sources_count}` — no
  `nodes`/`edges` payload. Consumers must treat a missing `unchanged` as a full response.
- Probe validity requires the caller to hold `include_archive`/`exclude_dirs` fixed between
  the full response and the probe (policy changes must use `force: true`); the probe does
  not re-check the cached filter policy.
- UI solve-state cache: the PLM graph tab persists settled force-layout positions to
  `<project>/.plm/graph_layout.json` (`{positions: {node_id: [x, y]}}`). UI-owned; does not
  participate in worker cache invalidation.

### Assembly decomposition tree (2026-07)
- New worker RPC `cad_graph_assembly_tree {project_dir, doc_id?, max_depth?=12} ->
  {ok, trees:[{id, label, kind, count, path?, children:[…]}]}` — nested assembly →
  subassembly/part decomposition over `CONTAINS bom_item INSTANCE_OF doc` chains.
  `count` = instance quantity within the parent. Roots: `doc_id` when given, else every
  assembly no other assembly instances (all assemblies on cyclic fallback). Parts are
  leaves; cycles and depth are capped.
- MCP: `cad_graph(query_kind="tree", node_id=<optional root>, project_dir)` exposes the
  same tree to agents.

### File nodes + keyword web + cross-project merge (2026-07-27)
- New node kinds `file` and `keyword`; new edge kind `TAGGED` (keyword → target).
- `file` nodes: non-CAD project files walked with the same skip/archive policy as
  containers, curated extension set: office (`.pdf .doc .docx .xls .xlsx .xlsm .ppt
  .pptx .csv`), text (`.md .txt .rst`), images (`.png .jpg .jpeg .svg .bmp .webp`),
  3D exchange (`.step .stp .iges .igs .stl .obj .3mf`), fab (`.gbr .drl`). Node
  `id="file:<rel>"`, `meta:{ext, size_bytes, mt, folder}`; capped at 500 per project
  (overflow reported as `files_truncated` on the graph). Vault manifest items match by
  path onto file AND doc nodes (`meta:{item_id, part_number, lifecycle}`). File-node
  mtimes participate in cache validation via the same `file:<rel>` key namespace.
- Keyword overlay `<project>/.plm/keywords.json`, schema `aicad.cad_graph_keywords/1`:
  `{schema, entries:[{id:"k<8hex>", target, keyword, origin:"manual"|"ai", author{},
  created_at}]}`. `target` is a node id or project-relative path (resolved to its
  file/doc node). Merge: one `keyword` node per distinct keyword (`id="kw:<kw>"`) +
  `TAGGED` edge per entry; targets get `meta.keywords` (sorted, ≤8). Missing file =
  empty overlay. Mutations invalidate the graph cache.
- Watch overlay `<project>/.plm/graph_watch.json`, schema `aicad.cad_graph_watch/1`:
  `{schema, watch:[{id:"w<8hex>", keyword?|node_id?, note, origin, created_at}]}` —
  "keywords of interest". Never changes the graph (no cache invalidation); consumers
  flag matching nodes (by node id, `kw:<keyword>` id, or `meta.keywords`).
- Graph config `<project>/.plm/graph_config.json`, schema `aicad.cad_graph_config/1`:
  `{office_content=false, office_max_chars=4000, office_kinds=[pdf,docx,xlsx,pptx,csv,
  doc,xlsm], linked_projects=[], project_key?}`. Zero-config defaults when absent;
  config mtime participates in cache invalidation.
- Office content extraction (only when `office_content` is true): each office-kind file
  node runs through the shared `extract_office` (contracts/office.md §2), capped at
  `office_max_chars`; meta gains `text_preview` (≤300 chars), `word_count`, or
  `extract_error: true` on failure (never fatal). Part-number-shaped tokens
  (`[A-Z]{2,5}-\d{3,5}`) matching a node path-stem or `part_number` emit
  `file REFERENCES <target>`, `origin=inferred`, `meta:{via:"office_mention", term}`,
  ≤20 per file.
- Cross-project: every node carries `meta.project` (owning project root). A node ref
  may be qualified `<project_key>/<local_id>` (`project_key` = config override or
  `"p"+sha1(abs path)[:8]`). `cad_graph_get {linked: true}` merges each
  `linked_projects` graph into the primary: ids stay local, `kw:` nodes with the same
  id intentionally merge (shared keyword = shared web), other collisions are prefixed
  `<project_key>/` on the non-primary copy; qualified overlay links in the primary
  resolve against the merged id space. Response adds `linked_projects: [root paths]`.
- Change diff: `cad_graph_changes {project_dir, linked?} -> {ok, nodes_added,
  nodes_removed, edges_added, edges_removed, watched, generated_at}` — set diff of the
  on-disk cached graph vs a fresh rebuild; added/removed nodes carry `watched: true`
  when matched by the watch overlay. `invalidate_cache` now renames the cache to
  `.plm/graph.prev.json` (not delete) so a mutation keeps the diff baseline.
- New worker RPCs (flat envelope, all take `project_dir`; query RPCs also take
  `linked?`):
  - `cad_graph_keyword_add {target, keyword, origin?, author?} -> {ok, entry}`
    (`invalid_keyword` / `invalid_target`; dedupe returns `{ok, entry, deduped}`).
  - `cad_graph_keyword_remove {entry_id?|target+keyword} -> {ok, removed}`.
  - `cad_graph_keyword_list {target?} -> {ok, entries}`.
  - `cad_graph_search {q?, kinds?, keyword?, linked?} -> {ok, nodes, edges,
    node_count, edge_count}` — substring over id/label/path/keywords/project + kind and
    keyword filters; edges are the induced subgraph; response capped 400/400.
  - `cad_graph_neighbors {node_id, depth?=1, linked?} -> {ok, nodes, edges, depth}` —
    undirected ego-graph, depth clamped ≤6.
  - `cad_graph_watch_add {keyword?|node_id?, note?, origin?} -> {ok, entry}`
    (`invalid_watch` when both/neither given), `cad_graph_watch_remove {entry_id}`,
    `cad_graph_watch_list -> {ok, watch}`.
  - `cad_graph_config_get -> {ok, config, project_key}`,
    `cad_graph_config_set {office_content?|office_max_chars?|office_kinds?|
    linked_projects?|project_key?} -> {ok, config}` (`invalid_field` /
    `invalid_project`).
- MCP (family `cad`, `services/mcp/graph_tools.py`): `plm_graph_keyword`,
  `plm_graph_search`, `plm_graph_neighbors`, `plm_graph_links` (list/remove),
  `plm_graph_watch`, `plm_graph_changes`, `plm_graph_configure`, `plm_graph_suggest`
  (heuristic link suggestions: same_folder / name_match / shared_keyword /
  content_mention, confidence-ranked), `plm_graph_brief` (one-node dossier).
  `cad_graph_link` src/dst accept cross-project qualified refs.

### Compressed derived cache and complete source freshness (2026-09-13)

- The derived cache storage paths above are superseded by `.plm/graph.json.gz`
  and `.plm/graph.prev.json.gz`. Both hold compact gzip-compressed JSON; the graph
  schema, node/edge content and RPC response shapes are unchanged.
- Legacy uncompressed caches are not authoritative and are regenerated from
  project sources on first use. Overlay files and UI layout files keep their
  existing paths and formats.
- Cache reads and writes admit at most 64 MiB compressed and 1 GiB expanded.
  Invalid, corrupt or oversized caches are misses, never partial graph results.
  Publication is atomic; invalidation retains the previous compressed graph for
  change-diff consumers.
- Freshness records every candidate source under the requested walk policy,
  including unreadable containers and files beyond the existing file-node display
  cap. Capturing this fingerprint before extraction prevents a large project from
  writing a cache that its own source scan immediately rejects.
- The cache additionally records internal source stat identities (nanosecond
  modification/change times, size, inode and mode) during that same scan.
  Restoring read permissions invalidates previously omitted source content without
  requiring an mtime change. Public mtime and source-count fields are unchanged.
