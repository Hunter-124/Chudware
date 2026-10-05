# graphify reference: query, path, explain, affected

Load this to answer questions from an existing `graphify-out/graph.json`. `query`, `path`, `explain`, `affected` and `god-nodes` default to that graph and accept `--graph <path>`. If it is missing, report that a build is needed (`/graphify <path>`) and stop. Questions never rebuild, recluster, rewrite `graph.json`, or automatically save feedback. The CLI may write its own local query log/orientation stamp; that is not a graph mutation.

## Step 0: Constrained query expansion (required before `query`)

`graphify query` scores nodes by IDF-weighted matching against node labels. It does not do synonyms, translation, or stemming, so a question worded differently from the graph's labels returns noise. Expand the query against the graph's own vocabulary without inventing tokens:

For a large graph, do not dump its whole vocabulary. Validate candidate words from the question against labels and print only matches. This snippet needs only stdlib Python, so a question does not require installation or interpreter sidecars:

```bash
python3 -c "
import json, re
from pathlib import Path
data = json.loads(Path('graphify-out/graph.json').read_text(encoding='utf-8'))
vocab = set()
for n in data['nodes']:
    for c in re.findall(r'[^\W\d_]+', n.get('label', '') or '', re.UNICODE):
        for p in re.findall(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+', c) or [c]:
            t = p.lower()
            if 3 <= len(t) <= 30:
                vocab.add(t)
candidates = 'CANDIDATE_WORDS'.lower().split()
print(sorted(set(candidates) & vocab))
"
```

Replace `CANDIDATE_WORDS` with relevant question words (and proposed translations/morphological forms). Only printed matches may be used. If needed, retry a different candidate set; never treat an unchecked synonym as evidence. Choose **up to 12 matching tokens**:
- Only tokens present in the printed matches. Never substitute an unverified synonym.
- Map cross-language terms and morphology only to tokens that exist (`handlers` → `handler` only if `handler` is present).
- Print `Query expanded to (from graph vocab, N tokens): [...]` before running the query.
- If nothing matches, say the graph has no relevant vocabulary and stop. Do not fabricate a search.

`path`, `explain` and `affected` take node names and do not need expansion. On ambiguity, use the reported full node ID or repo-relative path; do not guess an endpoint. A displayed `path` is a structural shortest path, not proof of executable control flow: preserve each hop's direction, relation and confidence exactly as printed.

## Commands

```bash
graphify query "TOKENS"                  # BFS, broad context (default budget 2000 tokens)
graphify query "TOKENS" --dfs            # DFS, trace one chain
graphify query "TOKENS" --budget 4000    # larger output cap
graphify query "TOKENS" --context C      # filter by edge context (repeatable)
graphify path "NODE_A" "NODE_B"          # shortest path with relation + confidence per hop
graphify explain "NODE"                  # node, source, degree, neighbours
graphify affected "NODE" --depth 2 [--relation calls]   # reverse traversal: what depends on NODE
graphify god-nodes --top 10 [--json]     # architectural hubs
```

Answer using **only** the command output: node labels, relations, confidence tags, `source_file`/`source_location`. Quote the location when citing a fact. Explain a path hop by hop. If the output is insufficient, say so, and only then read the cited source lines. Never add edges the graph does not contain.

## Feedback loop (optional, local only)

Record whether an answer helped, so later sessions start from the right nodes:

```bash
graphify save-result --question "ORIGINAL_QUESTION" --answer "ANSWER (incl. expanded tokens)" \
  --type query|path_query|explain --nodes "NODE1" "NODE2" \
  [--outcome useful|dead_end|corrected] [--correction "what was right"]
```

`graphify reflect --if-stale` is a deterministic local write (no LLM) that turns saved feedback into `graphify-out/reflections/LESSONS.md`. Run it only when feedback maintenance was requested, not as part of a read-only question. Existing lessons can guide source selection but are not evidence for an edge.
