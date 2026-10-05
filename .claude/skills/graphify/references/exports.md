# graphify reference: exports, tree, benchmark and MCP

These are operations on an existing graph, not `extract` flags. Slash-command export flags in SKILL.md map to the commands below. Run only requested exports, from the directory containing `graphify-out/`; they never require re-extraction. Keep `.graphify_labels.json` and `.graphify_analysis.json` as durable sidecars.

## File exports

```bash
graphify export html                         # graph.html; warn above 5,000 nodes
graphify export html --node-limit 5000       # aggregate a larger graph
graphify export obsidian                     # graphify-out/obsidian/
graphify export obsidian --dir /absolute/vault/path
graphify export wiki                         # requires nonempty .graphify_analysis.json
graphify export svg
graphify export graphml
graphify export neo4j                        # local Cypher artifact
graphify export falkordb                     # local Cypher artifact
graphify export callflow-html                # structural callflow report, not runtime trace
graphify tree                               # collapsible tree HTML
```

Exports accept `--graph PATH`; label-aware exports also accept `--labels PATH`. `tree` accepts `--graph PATH` and `--output HTML`. For a missing wiki analysis sidecar, `graphify cluster-only . --no-label --no-viz` regenerates community data without provider calls. Do not automatically recluster a read-only query to repair an export. Neo4j and FalkorDB use the same `cypher.txt` filename: preserve separate destinations if both artifacts are requested.

## Database pushes (explicit request only)

A push transmits graph contents and mutates an external database. Confirm the user-selected destination; never push as part of a local export or question. Use already configured environment credentials, not literal passwords in argv or chat:

```bash
graphify export neo4j --push bolt://localhost:7687 --user neo4j
graphify export falkordb --push falkordb://localhost:6379
```

The installed exporter reads `NEO4J_PASSWORD` or `FALKORDB_PASSWORD`. Do not read, print or request those values. If authentication is unavailable, deliver the local Cypher artifact instead. `MERGE` avoids duplicate identities on repeat pushes; it does not promise deletion reconciliation or a transaction-free overwrite. FalkorDB runs one OpenCypher statement at a time rather than importing a whole script with `cypher-shell`.

## MCP server (explicit request only)

```bash
"$(cat graphify-out/.graphify_python)" -m graphify.serve graphify-out/graph.json
```

This starts a long-lived stdio server, not an export file. Use the installed graphify interpreter; system Python may not import a uv-tool package. For client configuration, supply that interpreter's absolute path as `command` and `[-m, graphify.serve, /absolute/path/to/graphify-out/graph.json]` as separate string arguments. Clients do not evaluate `$(...)`. Do not silently modify client settings or replace an existing server. The server includes `query_graph`, `get_node`, `get_neighbors`, `get_community`, `god_nodes`, `graph_stats` and `shortest_path`; use its advertised tool schema rather than assuming this list is exhaustive.

## Benchmark

```bash
graphify benchmark
```

When requested, or after a build whose detected `total_words` exceeds 5,000, report the command's actual output. Token-reduction estimates are not measured downstream LLM savings. Small corpora need no automatic benchmark.
