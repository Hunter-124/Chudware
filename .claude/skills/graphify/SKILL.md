---
name: graphify
description: "Use for any question about a codebase, its architecture, file relationships, or project content — especially when graphify-out/ exists, where the question should be treated as a graphify query first. Builds and queries a persistent knowledge graph (code via local AST, docs/papers/images via an LLM) with god nodes, communities, and query/path/explain tools."
---

# /graphify

Turn a folder into a queryable knowledge graph: `graphify-out/graph.json` (graph), `GRAPH_REPORT.md` (audit report), `graph.html` (viz). Every edge carries provenance — `EXTRACTED` (explicit in source), `INFERRED` (with `confidence_score`), or `AMBIGUOUS`.

This skill and its eight references are maintained locally, reviewed against the installed `graphifyy` 0.9.31 package source and bundled Claude assets. `.graphify_version` records that compatibility review, not an unmodified bundle installation. Local rules deliberately retain read-only question handling, provenance, corpus-size gates, sensitive exclusions and explicit consent for lossy writes. Do not run `graphify install` (it overwrites this skill) or `graphify claude install` (it rewrites `CLAUDE.md` and the curated PreToolUse hooks in `.claude/settings.json`). After a package upgrade, review installed source/help before updating the compatibility stamp.

## Usage

```
/graphify "<question>"                     # existing graph → graphify query (fast path)
/graphify query "<question>" [--dfs] [--budget N] [--context C]
/graphify path "A" "B"                     # shortest path between two nodes
/graphify explain "X"                      # node + neighbours
/graphify affected "X" [--relation R] [--depth N]   # reverse impact
/graphify god-nodes [--top N]              # most connected hubs
/graphify update [<path>]                  # refresh code (AST, no LLM, no key)
/graphify [<path>]                         # build: code via AST; docs/papers/images need an LLM
/graphify <path> --code-only               # build code only (no LLM, no key)
/graphify <path> --mode deep               # richer INFERRED edges on the semantic pass
/graphify <path> --update                  # refresh code + re-extract changed docs/papers/images
/graphify <path> --cluster-only [--no-viz] # recluster existing graph, regenerate report/html
/graphify <path> --obsidian|--wiki|--svg|--graphml|--neo4j|--falkordb|--mcp   # exports
/graphify add <url> [--author N] [--contributor N]  # fetch a URL into ./raw
/graphify <path> --watch                   # auto-rebuild code on change
/graphify https://github.com/<owner>/<repo> [--branch B]   # clone, then build
```

`/graphify --help` or `-h` alone: print this Usage block verbatim and stop.

## Fast path — existing graph (default for questions)

If `graphify-out/graph.json` exists in the current working directory and the request is a question (not an explicit build/`--update`/`--cluster-only`/path/URL), answer from the graph immediately: no detection, no corpus-size check, no rebuild. Follow `references/query.md`:

1. Expand the question against the graph's own label vocabulary (only tokens that exist in the graph; none match → say so and stop).
2. `graphify query "<expanded tokens>"`; use `path`, `explain`, `affected`, `god-nodes` for those shapes.
3. Answer **only** from the command output; cite `source_file`/`source_location`; keep the edge's confidence tag. If the graph lacks the fact, say so — never invent an edge or node.

Read `graphify-out/GRAPH_REPORT.md` only for broad architecture review or when query/path/explain do not surface enough.

## Keep the graph current

```bash
graphify update .        # re-extract changed code via AST; no LLM, no API key
```

- Uses the scan root recorded in `graphify-out/.graphify_root` when no path is given.
- Shrink guard: refuses unexplained node loss; legitimate loss attributed to rebuilt or deleted sources can pass without `--force`. Pass `--force` (or `GRAPHIFY_FORCE=1`) **only** after the user accepts an intentional reduction; otherwise report the refusal.
- `update` does not perform LLM semantic extraction. It can quick-scan document structure and remove deleted sources while preserving existing semantic contributions. Use the `--update` flow in `references/update.md` for semantic changes.

## Build a graph

### 1. Interpreter and install

```bash
command -v graphify >/dev/null || uv tool install graphifyy
GPY=$(cat graphify-out/.graphify_python 2>/dev/null)
"$GPY" -c "import graphify" 2>/dev/null || GPY=$(head -1 "$(readlink -f "$(command -v graphify)")" | tr -d '#!')
"$GPY" -c "import graphify" && mkdir -p graphify-out && printf '%s' "$GPY" > graphify-out/.graphify_python
```

Use `"$(cat graphify-out/.graphify_python)"` for package-API snippets in the references. The query vocabulary snippet uses only stdlib and needs no sidecar. Never install with `graphify install`. Keep default output conventions (`graphify-out/`); if `GRAPHIFY_OUT` is set, reconcile all paths explicitly before a mutating workflow.

### 2. Detect and gate (default path `.`)

```bash
$(cat graphify-out/.graphify_python) -c "
import json; from pathlib import Path; from graphify.detect import detect
r = detect(Path('INPUT_PATH'))
Path('graphify-out/.graphify_detect.json').write_text(json.dumps(r, ensure_ascii=False), encoding='utf-8')
print({k: len(v) for k, v in r['files'].items() if v}, r['total_files'], 'files', r['total_words'], 'words', len(r['skipped_sensitive']), 'skipped as sensitive')
"
```

- `total_files == 0` → stop: "No supported files found in <path>."
- Sensitive-file exclusion is enforced by `detect` (secrets, keys, credentials) plus `.gitignore`/`.graphifyignore`. Report the skipped **count only**, never the names or contents; do not paste sensitive-name diagnostics from CLI output. Never re-include excluded files or pass `--no-gitignore` to get around it. If the user suspects a wrongly flagged file, they can rename/move it.
- `total_files > 500` or `total_words > 2,000,000` → show the warning, list the top 5 first-level subdirectories by file count, and ask which one to build (no subfolders → suggest `--no-cluster`). Never skip this warning.
- Video/audio present → use `references/transcribe.md` after the semantic cache check, before dispatching uncached media.

### 3. Extract

**Code needs no API key.** Code is extracted by local AST with no LLM. Docs, papers, and images need an LLM. Never ask the user for a key, never read, print, or echo one, and never block on one.

Run from the directory that should hold `graphify-out/`. `--out .` keeps the output there even when `INPUT_PATH` is a subfolder.

- **Code only** (no docs/papers/images, or `--code-only`):
  ```bash
  graphify extract INPUT_PATH --code-only --out .
  ```
- **Docs/papers/images/media, host-agent route (default, no key):** this session is the LLM. Build code first with the line above **only if code exists**; a docs/media-only corpus starts directly with the semantic pass in `references/update.md`. It uses `references/extraction-spec.md`, then the real `cache-check`/`merge-chunks`/`merge-semantic` operations and a shrink-guarded merge. `--mode deep` uses the deep cache namespace and sets `DEEP_MODE=true` for every subagent.
- **Docs/papers/images, provider route (only if the user asks for it):** `graphify extract INPUT_PATH --out . [--backend B] [--mode deep]`. Backend names are `gemini`, `kimi`, `claude`, `openai`, `deepseek`, `azure`, `bedrock`, `ollama`, `claude-cli`, or a registered custom provider. Automatic selection tries Gemini, Kimi, Anthropic, OpenAI, DeepSeek, Azure, AWS Bedrock, opt-in Ollama, then custom providers with configured keys; `claude-cli` requires explicit selection and the authenticated Claude CLI, not a Gemini key. Prefer an explicit backend to avoid unintended routing. If none is usable, report the failure and use the host-agent route; never request or expose a credential. See `references/github-and-merge.md` for provider configuration. The bare `extract` provider route does not transcribe media; use the host-agent media flow.

`extract` is incremental after the first build and refuses an empty graph. If extraction is incomplete, it refuses a smaller partial replacement; `--allow-partial` overrides that refusal only with the user's acceptance. A complete extraction can legitimately write a smaller graph without that override: review the requested root and reported losses instead of assuming every CLI write has a blanket shrink guard. `--force` re-scans everything and skips semantic cache reads, potentially re-spending tokens.

### 4. Cluster, report, label

```bash
graphify cluster-only . --no-label            # communities + GRAPH_REPORT.md + graph.html, no provider
graphify cluster-only . --no-label --no-viz   # skip graph.html (very large graphs)
```

Use `--no-label` by default: valid saved labels are reused, otherwise names are local placeholders/hub labels, without provider calls. Without it, a first labeling pass can auto-select a configured provider. `graphify label .` forces a labeling attempt (hub fallback if no backend), and `--missing-only` limits it to missing names. Use provider labeling only when the user chooses it; node labels are sent externally. Warn before HTML generation above 5,000 nodes (HTML export aggregates large graphs).

### 5. Report

Summarize: output paths; node/edge/community counts; tokens spent on semantic extraction (subagent usage or the `extract` token line); the sensitive-skip count; any guard refusal or health warning (`graphify diagnose multigraph` for edge-collapse risk). Paste only the God Nodes, Surprising Connections and Suggested Questions sections from `GRAPH_REPORT.md`, then offer to trace the most cross-community suggested question with `graphify query`.

## Other operations

- `--update` (code plus changed docs) and `--cluster-only`: `references/update.md`.
- Exports (`html`, `obsidian`, `wiki`, `svg`, `graphml`, `neo4j`, `falkordb`, `callflow-html`), `tree`, `benchmark`, MCP server: `references/exports.md`.
- `add <url>` and `--watch`: `references/add-watch.md`.
- Git hooks (`graphify hook install|status`) and why not to run `claude install` here: `references/hooks.md`.
- GitHub URLs, multiple repos, monorepo subfolders (`clone`, `merge-graphs`): `references/github-and-merge.md`.

## Honesty rules

- Never invent an edge or node; if unsure, use `AMBIGUOUS`. Answers cite the graph output only.
- Never skip the corpus-size warning. Never hide cohesion scores; show the raw number.
- Always report token cost for any LLM extraction.
- Never bypass the empty-graph or shrink guards silently. `update --force` (accepts a shrink) and `extract --allow-partial` (accepts a partial graph) need the user's explicit acceptance.
- Never surface sensitive file names or contents. Never send corpus content to an external provider unless the user chose the provider route.
