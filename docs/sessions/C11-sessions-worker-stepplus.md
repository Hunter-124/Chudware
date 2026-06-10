# C11 — Doc sessions + ops application + worker doc_* RPC + minimal .step+

Model: **opus** · Wave 1 · Depends: C01, C02, C03, C09 · Follow `docs/sessions/README.md`.

## Mission
Wire everything into the live worker: document sessions with revisions and undo, the full op
vocabulary, tessellation on rebuild, pick resolution, and minimal `.step+` save/open. After this
card, the C++ side has a complete backend for M1.

## Read first
1. `docs/contracts/doc_ops.md` (op vocabulary, envelope, transactionality, placeholders, errors)
2. `docs/contracts/worker_rpc.md` §2 (every doc_* method + protocol 2)
3. `docs/contracts/stepplus.md` (§1 required entries, §2 load order, §3 atomic save)
4. `engine/worker.py` (serve loop you extend), `engine/doc/compiler.py` (C01),
   `engine/doc/tessellate.py` (C02), `engine/doc/selectors.py` (C03), C09's features
5. `engine/test_worker_stream.py` (the wire-test style you extend)

## Owned paths
`engine/doc/session.py` · `engine/doc/ops.py` · `engine/doc/stepplus.py` ·
`engine/doc/test_ops.py` · `engine/doc/test_stepplus.py` · `engine/doc/test_worker_doc.py` ·
**EXCEPTION: you may edit `engine/worker.py`** (you are the only wave-1 agent touching it) —
keep existing methods byte-identical, append new handlers + bump ping to `protocol: 2`.

## Deliverables
1. `ops.py`: validate-then-apply per doc_ops §2 (atomic batch; `$N`/`$self` placeholders;
   id assignment f<N>/e<N>/c<N>/k<N>/m<N> monotonic per doc; dependency checks for delete/
   reorder using a ref-scan helper; patch semantics for edit ops). Reuses
   `engine.doc.schema` validators — reject batches whose RESULTING document would not validate.
2. `session.py`: `DocSession {doc_id, doc, revision, undo_stack (bounded 100 doc-JSON
   snapshots), redo_stack, compile_cache, last_result}` + module-level registry;
   `apply_ops(...) -> ops-response dict` (doc_ops §1 exactly: revision, id_map, tree,
   rebuild.feature_status, tess with AICM written to `%APPDATA%\AI-CAD\cache\docs\
   <doc_id>_r<rev>.aicm`, bodies list); `undo/redo`; `resolve_pick(body_id, kind, topo_id)
   -> selector` (C03 describers + the compile result's owner tags); emit `doc_progress`
   events past 300 ms.
3. `stepplus.py`: `save(session, path)` / `open(path) -> DocSession` per the contract:
   ZIP with thumbnail.png (render the tessellation via `engine/render.py` helpers on the
   exported STL, or a 256² flat placeholder if that's heavy — note the choice), meta.json,
   document.json, geometry.step (export via build123d exporters from the last good bodies),
   atomic tmp-then-replace. Open: parse → validate → rebuild; on rebuild failure return the
   doc with error statuses (do NOT implement the frozen-geometry fallback tonight; report).
4. `engine/worker.py` additions: `doc_new/doc_open/doc_save/doc_close/doc_list/doc_apply_ops/
   doc_undo/doc_redo/doc_tree/doc_rebuild/doc_resolve_pick` per worker_rpc.md, structured
   error envelope `{code, message, detail}` for new methods, `stale_revision` enforcement.
5. Tests:
   - `test_ops.py`: placeholder batch (add sketch + entities in one batch), patch edit, delete
     dependency refusal + cascade, reorder validation, stale revision, eq_set re-resolve
   - `test_stepplus.py`: save→open roundtrip preserves doc JSON + revision resets cleanly;
     unknown zip entries preserved on resave
   - `test_worker_doc.py`: REAL subprocess (mirror test_worker_stream style): ping protocol 2,
     doc_new part → add box via ops → tree shows it → apply fillet with a selector picked via
     doc_resolve_pick → undo → redo → save .step+ → doc_open it → doc_list; assert AICM file
     exists and parses (C02 `read_aicm`)

## Constraints
- Legacy methods and their tests must remain untouched and green.
- All canonical units; no GUI assumptions.

## Verification
Your three suites green PLUS `engine.test_worker_stream`, `engine.doc.test_compile`,
`engine.doc.test_features1`, `engine.doc.test_schema_fixtures` all still green.
