# graphify reference: clones, separate graphs and providers

## GitHub clone

```bash
graphify clone https://github.com/OWNER/REPO --branch BRANCH
```

Omit `--branch` for the default branch. The CLI prints the local path and caches clones under `~/.graphify/repos/OWNER/REPO`; inspect reported errors before using that path. Reusing a cached clone is not proof that it contains the latest remote revision. Run the ordinary detect/size/sensitive gates on the resolved local path before extraction.

## Multiple repos or subfolders

Never run several narrowed scans against the same existing parent `graphify-out/`. Use separate output directories. The CLI's default output is inside the scanned path, while the host-agent flow uses the current working directory; run each host-agent build from its own intended output parent.

```bash
graphify extract ./core --code-only             # ./core/graphify-out/graph.json
graphify extract ./services --code-only         # ./services/graphify-out/graph.json
graphify cluster-only ./core --no-label --no-viz
graphify cluster-only ./services --no-label --no-viz
graphify merge-graphs \
  ./core/graphify-out/graph.json \
  ./services/graphify-out/graph.json \
  --out graphify-out/cross-repo-graph.json
```

Use the host-agent semantic flow for docs/media, or a provider extraction only if the user chose it. `merge-graphs` namespaces inputs and attaches a `repo` attribute. Query the result explicitly with `--graph graphify-out/cross-repo-graph.json`. A merged graph is not a single-root incremental index: refresh its constituent graphs, then merge again. Default to a **separate output**, not replacing the live parent graph with a subset union. Preserve all needed inputs and obtain explicit acceptance before any intentional overwrite/reduction.

## Backend versus provider commands

`--backend NAME` selects an extractor/labeler backend. There is no `graphify backend` command in this package. Built-ins are `gemini`, `kimi`, `claude`, `openai`, `deepseek`, `azure`, `bedrock`, `ollama` and `claude-cli`; registered custom providers are also accepted. Names are not interchangeable: `claude` is the Anthropic API route; `claude-cli` invokes an already authenticated Claude CLI. Ollama is opt-in (explicit selection or its configured environment URL) and is considered after paid-provider credentials during auto-detection. See SKILL.md for automatic priority and the no-credential-exposure rule.

The actual configuration interface is:

```bash
graphify provider list
graphify provider show NAME
graphify provider add NAME --base-url URL --default-model MODEL --env-key ENV_VAR_NAME
graphify provider remove NAME
```

`provider list/show` concern custom providers, not the full built-in backend registry; `show` can reveal endpoint/configuration information, so do not paste it indiscriminately. `add/remove` mutate user-global provider configuration and require an explicit configuration request. `--env-key` names an environment variable; it does **not** take the secret value. Never print/read credentials, put them in command arguments, alter an existing provider silently, or infer permission to send corpus contents from the presence of a configured key. Local host-agent extraction and AST indexing remain available without provider setup.
