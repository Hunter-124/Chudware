## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## Autonomous sprint work (autopilot)

If the user says anything like "start working", "chip away", "find something to do",
or "pick up a task": read `work/prompts/autopilot.md` and follow it. In short — claim
an unclaimed ready task with `python3 work/tools/work.py claim --owner <agent-name>`
(atomic; safe with multiple agents running), execute the emitted work order from
`python3 work/tools/work.py prompt <id>` on its own branch/worktree, stop at
status: review for human merge, then claim the next. Never merge to master or mark
tasks done yourself. `python3 work/tools/work.py board` shows what other agents hold.
