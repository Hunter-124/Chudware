## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## Task board (solo)

Solo dev now — main WIP happens on this machine, remote is mainly a backup. If the
user says "start working", "chip away", or "find something to do": use
`python3 work/tools/work.py ready` to see unblocked tasks and `work/tools/work.py
board` for the full board. No claim/coordination step is needed since there's only
one contributor; just pick a task, work it, and mark it done yourself when finished.
