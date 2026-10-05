# graphify reference: add a URL and watch a folder

Load only for `/graphify add <url>` or an explicit `--watch` request. Neither runs during a graph question or a default build.

## Add

```bash
graphify add URL --dir ./raw                  # optional --author NAME --contributor NAME
```

`add` fetches and saves an input; it does not extract or merge it into `graph.json`. On failure report the error and stop. On success, run the host-agent semantic update in `references/update.md` using the **existing recorded scan root**, provided it includes `raw/`. Do not change a parent graph's root to `./raw`: a subset scan can misclassify unrelated indexed sources as excluded. If `raw/` is outside the recorded root, build a separate graph and use `merge-graphs` into a separate output, or obtain the user's choice of a broader root.

Supported inputs include webpages (Markdown via html2text), Twitter/X (oEmbed), arXiv (abstract and metadata), PDFs and images. Video URLs use yt-dlp and local Whisper, requiring the installed package's video dependencies; see `references/transcribe.md`. Image extraction uses this session's vision capability on the host-agent route, not necessarily Claude or Gemini. Preserve ingested frontmatter (`source_url`, `captured_at`, `author`, `contributor`) on graph nodes. Fetching a URL is network access, but does not authorize sending the rest of the corpus to an LLM provider. Never bypass the package URL/security validation.

## Watch

```bash
"$(cat graphify-out/.graphify_python)" -m graphify.watch INPUT_PATH --debounce 3
```

The `graphify watch INPUT_PATH` CLI is also available; the Python module accepts the explicit debounce setting. Run from the directory containing the graph to update and retain its original scan root. This is a long-lived watcher: start only when requested, retain ownership of its process, and stop with Ctrl+C or terminate only the watcher you started. Do not interrupt another user's watcher/session.

Code changes trigger local AST rebuilding and reporting, with no LLM or API key. Document/paper/image changes signal that a semantic update is needed; the watcher does not perform the host-agent extraction for you. Debouncing waits for a quiet interval so a group of edits is not rebuilt per file. Preserve shrink guards and sensitive/ignore exclusions. If a rebuild refuses unexplained loss, report it; never turn on `GRAPHIFY_FORCE` globally to silence the refusal.
