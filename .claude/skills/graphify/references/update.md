# graphify reference: semantic builds, updates and clustering

`/graphify <path> --update` is a **host-agent workflow**, not a `graphify extract --update` option. `graphify update [path]` is the installed local AST operation: no LLM or API key. Keep the recorded scan root; never use a narrowed subset to refresh an existing parent graph. All snippets below run from the output parent containing `graphify-out/`. Substitute `INPUT_PATH` with that original scan root. Use the installed interpreter recorded by SKILL.md; do not run install commands to repair a documentation mismatch.

## Prepare

For an existing graph, run `graphify update INPUT_PATH` for local code changes. It preserves semantic contributions, can quick-scan document structure and reconciles deletions. Its guard rejects unexplained node loss, not every smaller graph. Report refusals; `--force` or ambient `GRAPHIFY_FORCE` must not bypass them without explicit acceptance. For a first build, run `graphify extract INPUT_PATH --code-only --out .` **only if code exists**; docs/media-only corpora start below without an empty AST build.

Detect the current corpus and semantic changes:

```bash
"$(cat graphify-out/.graphify_python)" -c "
import json
from pathlib import Path
from graphify.detect import detect_incremental
r = detect_incremental(Path('INPUT_PATH'), kind='semantic')
Path('graphify-out/.graphify_detect.json').write_text(json.dumps(r, ensure_ascii=False), encoding='utf-8')
print({t: len(v) for t, v in r['files'].items() if v}, r['total_files'], 'files', r['total_words'], 'words', len(r.get('skipped_sensitive', [])), 'sensitive skips')
print(len(r.get('deleted_files', [])), 'deleted;', len(r.get('excluded_files', [])), 'excluded')
"
```

Apply SKILL.md's corpus-size warning and sensitive exclusions before dispatch. Report only sensitive-skip counts, never the names in detection or CLI diagnostics. We check **all live semantic files** against the content/prompt/mode cache: a changed extraction prompt or first deep pass must not be suppressed by unchanged manifest hashes. `deleted_files` and `excluded_files` are removal candidates, not re-extraction inputs; review their scope before pruning. If there are no semantic files or removals, skip to clustering.

## 1. Cache check

Set the absolute path of this skill's `references/extraction-spec.md` as `GRAPHIFY_SPEC_PATH`. Set `GRAPHIFY_SEMANTIC_MODE` to `deep` for `--mode deep`, otherwise the empty string. These are non-secret workflow parameters.

```bash
export GRAPHIFY_SPEC_PATH=/absolute/path/to/references/extraction-spec.md
export GRAPHIFY_SEMANTIC_MODE=""    # or deep
"$(cat graphify-out/.graphify_python)" -c "
import json
from pathlib import Path
d = json.loads(Path('graphify-out/.graphify_detect.json').read_text(encoding='utf-8'))
files = [f for t in ('document', 'paper', 'image', 'video') for f in d['files'].get(t, [])]
Path('graphify-out/.graphify_semantic_files.txt').write_text('\n'.join(files), encoding='utf-8')
print(len(files), 'semantic inputs')
"
rm -f graphify-out/.graphify_cached.json graphify-out/.graphify_semantic_new.json graphify-out/.graphify_transcripts.json graphify-out/.graphify_chunk_*.json
graphify cache-check graphify-out/.graphify_semantic_files.txt --root . --prompt-file "$GRAPHIFY_SPEC_PATH"
# Deep mode: add --deep to the same cache-check command.
```

The output is `.graphify_uncached.txt` plus `.graphify_cached.json` when there are hits. The installed cache can replay legacy entries that predate prompt fingerprinting, with a warning: report their unknown prompt provenance, never claim they were produced by this spec or automatically force a costly re-extraction. Clearing run-owned temporary outputs prevents an all-cache or failed run from replaying stale new chunks; never remove `graph.json`, the cache directory, labels, analysis or another active run's files. Do not run overlapping semantic passes against one output directory.

For uncached `video` inputs, use `references/transcribe.md` now. It writes a mapping from original media paths to readable transcript paths. Cache identity and graph provenance stay attributed to the original media, so edits/deletions reconcile correctly.

## 2. Extract uncached files

Partition `.graphify_uncached.txt` into directory-coherent chunks of roughly 20–25 files; give each image its own chunk. Dispatch write-capable extraction subagents together using the complete prompt in `extraction-spec.md`, filling `FILE_LIST` (absolute original source paths), `SCAN_ROOT` (the absolute original scan root), `MEDIA_TRANSCRIPTS` (the media-to-transcript mapping, or `{}`), `CHUNK_NUM`, `TOTAL_CHUNKS`, `DEEP_MODE` and an absolute `CHUNK_PATH`. Report the actual input/chunk count. Do not ask a read-only explorer to write a chunk.

Every expected chunk must exist and validate. A successful agent message without its chunk is not success. Missing/invalid/partial output must be retried or reported; do not silently merge a subset. If the user explicitly accepts a partial pass, omit failed files and leave their old contributions and semantic hashes pending. Do not treat partial fragments as complete cache entries. Record real input/output token usage in the chunk; if unavailable report it as unavailable, never invent zero usage.

```bash
graphify merge-chunks 'graphify-out/.graphify_chunk_*.json' --out graphify-out/.graphify_semantic_new.json
"$(cat graphify-out/.graphify_python)" -c "
import json, os
from pathlib import Path
from graphify.cache import save_semantic_cache
new = json.loads(Path('graphify-out/.graphify_semantic_new.json').read_text(encoding='utf-8'))
unc = Path('graphify-out/.graphify_uncached.txt').read_text(encoding='utf-8').splitlines()
print('cached', save_semantic_cache(new.get('nodes', []), new.get('edges', []), new.get('hyperedges', []), root=Path('.'), allowed_source_files=unc, prompt_file=os.environ['GRAPHIFY_SPEC_PATH'], mode=os.environ.get('GRAPHIFY_SEMANTIC_MODE') or None), 'files')
"
```

Run these only when fresh complete chunks exist. `merge-chunks` validates size/IDs, skips invalid chunks with warnings and refuses when none are valid; a zero exit code with skipped chunks is **not** complete acceptance. If all inputs were cached, skip this step and leave the new-result path absent. Cache reads/writes must use the same output-parent root, absolute source paths, prompt and mode.

## 3. Merge safely

```bash
graphify merge-semantic --cached graphify-out/.graphify_cached.json --new graphify-out/.graphify_semantic_new.json --out graphify-out/.graphify_semantic.json
"$(cat graphify-out/.graphify_python)" -c "
import json
from pathlib import Path
from graphify.build import build_merge
from graphify.cluster import cluster
from graphify.export import to_json
from graphify.detect import save_manifest
from graphify.cli import _stamped_manifest_files
p = Path('graphify-out/graph.json')
old = json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
sem = json.loads(Path('graphify-out/.graphify_semantic.json').read_text(encoding='utf-8'))
det = json.loads(Path('graphify-out/.graphify_detect.json').read_text(encoding='utf-8'))
prune = list(dict.fromkeys(det.get('deleted_files', []) + det.get('excluded_files', []))) or None
G = build_merge([sem], graph_path=p, prune_sources=prune, root=Path('INPUT_PATH'), directed=bool(old.get('directed', False)), dedup=False)
if G.number_of_nodes() == 0:
    raise SystemExit('ERROR: merged graph is empty; existing graph left untouched')
if not to_json(G, cluster(G), str(p), force=False):
    raise SystemExit('ERROR: shrink refused; existing graph and manifest left untouched')
files = {t: det['files'].get(t, []) for t in ('document', 'paper', 'image')}
# Media results carry the original video source; require output before stamping too.
files['document'] = files['document'] + det['files'].get('video', [])
stamped = _stamped_manifest_files(files, sem, Path('INPUT_PATH'))
done = {f for fl in stamped.values() for f in fl}
cleared = {f for fl in files.values() for f in fl} - done
corpus = {f for fl in det['files'].values() for f in fl}
save_manifest(stamped, kind='semantic', root=Path('INPUT_PATH'), scan_corpus=corpus, clear_semantic=cleared or None)
Path('graphify-out/.graphify_root').write_text(str(Path('INPUT_PATH').resolve()), encoding='utf-8')
print('graph:', G.number_of_nodes(), 'nodes,', G.number_of_edges(), 'edges;', len(done), 'files stamped,', len(cleared), 'pending')
"
```

`build_merge` reads the existing graph and replaces only re-extracted sources; absent sources remain, and `prune_sources` removes reviewed deleted/excluded sources. `dedup=False` avoids an incidental fuzzy-dedup loss of existing identities. Edge provenance/direction and untouched hyperedges are retained by the package merge/export APIs, not by a hand-written graph rewrite. The prior directed flag is retained; for an explicitly requested directed **first** build, use `directed=True`.

An empty merged graph always stops. A legitimate semantic reduction can still trigger the node-count shrink guard; do not automatically set `force=bool(prune)`. Report old/new counts and cause, obtain explicit acceptance, then rerun this merge with `force=True` only for that accepted reduction. Never stamp the manifest after a refused write. Only sources producing complete output are stamped; omitted inputs remain pending. Pure-deletion runs can merge empty semantic inputs, but cannot authorize a shrink by themselves.

## 4. Cluster and finish

```bash
graphify cluster-only . --no-label            # graph, analysis, report and HTML
graphify cluster-only . --no-label --no-viz   # omit expensive HTML
```

These are self-contained; do not rerun deleted intermediate-file steps. Preserve durable labels, analysis, cache, manifest and interpreter/root sidecars. `--no-label` prevents implicit provider calls. Without it, a first naming pass can auto-select a provider; `graphify label . --backend NAME [--missing-only]` explicitly attempts provider naming and falls back to hub labels if no backend is available. Use provider labeling only with the user's choice, since node labels leave the local workflow.

Report counts before/after, pending/failed inputs, real semantic/label token usage (or unavailable usage), raw cohesion scores and guard warnings. Run only requested exports. Remove this pass's `.graphify_detect.json`, `.graphify_semantic*.json`, `.graphify_semantic_files.txt`, `.graphify_cached.json`, `.graphify_uncached.txt`, `.graphify_transcripts.json` and `.graphify_chunk_*.json` only after a successful handoff; keep failed-pass evidence for recovery. Do not delete durable transcript text or manually rewrite the parent graph.
