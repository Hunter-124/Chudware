# Contract: `.step+` container — v1

> Chudware's native file. A ZIP that travels like a STEP file: any other CAD opens the embedded
> `geometry.step`; Chudware gets the full parametric document, studies, and AI memory back.

## 1. ZIP layout

| entry | required | contents |
|---|---|---|
| `thumbnail.png` | yes | 256×256 offscreen render; **STORED (uncompressed), first entry** so shell previewers can read it cheaply |
| `meta.json` | yes | `{"container": "aicad.step+/1", "app_version": "…", "kind": "part"\|"assembly", "created": iso8601, "modified": iso8601, "units": {"system": …}, "name": …}` |
| `document.json` | yes | the full `aicad.part/1` or `aicad.asm/1` document (doc_schema.md) |
| `parts/<uuid>.json` | asm only | embedded component part documents referenced via `source.embedded` |
| `geometry.step` | yes* | AP214 snapshot of the **last successful rebuild** (all bodies). *Omitted only if the document has never rebuilt successfully |
| `studies/<id>.json` | no | simulation study definitions (material, fixtures, loads, mesh density, type) |
| `studies/<id>/results.npz` | no | cached results; **cap 20 MB per study** — larger results are dropped on save with a note (re-run to regenerate) |
| `studies/<id>/overlay.aicm` | no | result overlay chunks for instant display on reopen |
| `ai/memory.json` | no | `{"design_intent": str, "decisions": [{"when": iso8601, "what": str, "why": str}], "conversation_digests": [str], "named_dims": {label: "f5.params.depth"}}` — written by the M9 memory role, read by intake on follow-up sessions |

## 2. Load order (normative)

1. Read `meta.json`; reject unknown major container version.
2. Parse `document.json` → full rebuild.
3. If the rebuild fails (e.g. version skew, kernel change): offer the user `geometry.step` as a
   frozen `import` feature instead, preserving the broken document JSON untouched on disk.
4. `studies/*` load lazily when the Simulation tab opens; cached overlays display immediately
   with a "cached — re-run to refresh" tag.

## 3. Rules

- Writers MUST preserve unknown ZIP entries on resave (forward compatibility).
- `document.json` numbers are canonical units (mm/deg/g) like everywhere else.
- Extension is literally `.step+`. Save As also offers plain `.step` (geometry only) with a
  one-time "parametrics are not included in plain STEP" notice.
- Atomic save: write through an exclusively created file in a new owner-private
  staging directory, then publish that open file over the destination; no fixed
  `<name>.tmp` path is opened. Archive members are matched case-sensitively.

## 4. Persisted-geometry cache entries (additive, 2026-07-12)

Optional, display/build-acceleration only; `document.json` stays the sole source of truth.
All are written by `doc_save` ONLY after a fully-ok rebuild and are erased first on every
save, so combined with atomic save a crash mid-rebuild can never persist a half-built cache.
Readers validate `cache.json`'s `engine` (engine+cache-schema key) + `doc_hash` (FNV-1a of
canonical `document.json`, volatile `id` stripped) + `solved: true` and treat ANY mismatch
or parse error as a cache miss (full rebuild). Writers that don't understand these entries
just preserve them (rule §3.1) or drop them — both are safe.
The native engine key includes a geometry/topology semantic epoch as well as the
OCCT version. Changes to compiled geometry or selectable topology must invalidate
that epoch even when the source document and OCCT version are unchanged. An older
epoch is a cache miss, never a reason to relax native selector resolution.

| entry | doc kind | contents |
|---|---|---|
| `cache.json` | part + asm | header `{engine, doc_hash, solved: true, response}`; for assemblies also `components`: fingerprints `[{path, size, mtime}]` of every non-suppressed component's resolved source file — reopen replays the cache only if ALL still match |
| `cache.aicm` | part + asm | the saved tessellation (AICM blob), STORED uncompressed |
| `cache.brep` | part | compiled bodies as `"AICBREP1"` magic · u64 count · per-body u64 length + BinTools stream (**no triangulation** — BinTools-restored triangulation triggers runaway BRepMesh refinement on NURBS faces, OCCT 7.9); lets an assembly load this part as a component without re-running `compile_document` |

Optional `.step+` mesh caches are admitted only after source and unknown entries.
STORED `cache.aicm` blobs may use the remaining 256 MiB aggregate archive budget;
compressed mesh caches retain the 64 MiB per-entry and compression-ratio caps.
A cache that does not fit is skipped before allocation/decompression, and writers
omit mesh caches that exceed the budget left by all other payloads. Entry-count,
per-entry, aggregate-expansion and compression-ratio limits remain unchanged for
source documents and unknown entries; caches cannot crowd out that data.

## 5. Nonbinding recovery snapshots (2026-09-12)

Native `doc_save(..., preserve_path=true)` produces the same container format
from cached scratch without rebinding or clearing the live document. Relative
component paths in that snapshot resolve against the live document's original
directory, not the recovery directory. Unknown entries remain preserved;
discarded geometry/cache entries are regenerated under the existing rules.
This does not embed external component files or make an assembly portable by
itself. Ordinary saved relative-source bundles still require those files to
travel with the containing assembly. See `doc_ops.md` §24.

## 6. Selected-face display-cache epoch (2026-09-12)

The native geometry/display semantic key is now `chudware-geomcache/3` plus
the OCCT version. Epoch 3 invalidates caches produced before selected-face
finishes and shared cylindrical bore/boss fingerprints were supported.
An older writer may preserve unknown authored appearance fields while
retaining an unpainted display cache; matching document content alone is
therefore insufficient. Reopen must treat the older epoch as a cache miss,
then compile the authoritative source appearance without requiring a later
user edit or lazy rebuild to correct the initial display.

## 7. Exact-fingerprint display-cache epoch (2026-09-12)

The current semantic key is `chudware-geomcache/4` plus the OCCT version.
Epoch 4 extends §6: exact selected-face fingerprints can independently paint
faces that share a coarse short SID. Older display caches must not hide this
authored distinction. Invalidated caches rebuild from the native source;
neither an ordinal guess nor a rewritten old engine identity is permitted.

## 8. Owner-attribution cache epoch (2026-09-27)

The current semantic key is `chudware-geomcache/6` plus the OCCT version.
Epoch 6 re-attributes `owner_map` faces/edges through operation history (a
dress-up or boolean feature owns only the faces it generates); cached display
responses from epoch 5 carry the old owner map and are a cache miss.

## 9. Build checkpoints, warm open and cache diagnostics (2026-10-05)

Additive to §4; the engine key stays `chudware-geomcache/6` (no stored format of an
existing entry changed). Files without the new entry still open warm from `cache.brep`.

| entry | doc kind | contents |
|---|---|---|
| `cache.ckpt` | part | the incremental-rebuild checkpoints: `"AICCKPT1"` magic · one CBOR document `{format:1, engine, doc_hash, quality, chain:[u64], snaps:[…], full, results, statuses, owners, tool_recs, blobs, strings, nshapes, brep}`. `chain` is the compile-input hash chain (h0 = env + active configuration + display tier + compile mode; h[i+1] = h[i] folded with feature i's JSON). Each snap is the compiler's complete loop-carried state after feature i. Every shape of every snap and of `full` lives in ONE BinTools shape set (`brep`, format VERSION_3, **no triangulation**), so TShape/geometry/location sharing — and with it TopExp ordinals and owner attribution — round-trips exactly. `full` is the identical-inputs result (statuses, bodies, owner map, datums, tool records, side channels) plus `aicm_size`/`aicm_hash` (FNV-1a) of the `cache.aicm` it was built with. STORED uncompressed |

Writing (`doc_save`, parts only):

- Written on every save, including a partially failed rebuild: only checkpoints after an
  unbroken run of successful features are kept (the failed feature is re-attempted on
  reopen, never replayed from a recorded failure); `full` only when every feature succeeded.
  `cache.json`/`cache.aicm`/`cache.brep`/`geometry.step` keep the fully-ok gate of §4.
- `cache.aicm` then `cache.ckpt` are admitted into the archive budget left by every other
  entry (the reader's same remainder). An over-budget `cache.ckpt` drops its OLDEST half of
  checkpoints repeatedly (the deepest serve the tail edit), then `full`, until it fits; an
  over-budget `cache.aicm` is omitted and `cache.json` records `"aicm": "budget"`.
- A geometry cache above the 64 MiB compressed per-entry cap is written STORED.

Reading (`doc_open`, parts):

- The display cache (`cache.json` + `cache.aicm`) is validated in this order: present →
  parses → `engine` → `doc_hash` → `solved` → `display_quality` → mesh present.
- `cache.ckpt` is validated independently: `engine` must match; then each checkpoint is
  content-addressed — only the prefix whose stored `chain` equals the chain recomputed
  from the opened `document.json` (at the saved tier) is restored, re-keyed to the
  session's display tier. `full` is restored only for an identical chain + appearances at
  the same tier and a byte-identical `cache.aicm`. So checkpoints also serve a document
  edited outside Chudware (matching prefix) or opened at another display tier (re-mesh only).
- On a display hit the session's compile is hydrated (from `full`, else from `cache.brep` +
  the saved response + `cache.aicm` body names, flagged partial so tool-record/weldment/
  flat-pattern readers compile once): the open and the first activate/pick/measure/save
  execute no feature, and the first edit executes only the features after the deepest
  restored checkpoint. On a display miss with usable checkpoints the open compiles from
  them (re-mesh only when the whole chain matched). A tier mismatch without checkpoints
  re-meshes the saved `cache.brep` bodies instead of recompiling.
- A geometry cache entry beyond the reader limits (per-entry, aggregate, compression
  ratio) or that fails to extract is skipped and reported; it never fails the open.

Result fields (additive):

- `doc_open` → `cache_hit` (bool, display served from the cache), `warm` (bool: the session
  holds the current compile and no feature executed during the open; assemblies: false on a
  hit, whose `asm_result` is rebuilt lazily), `cache_miss_reason` (only when `cache_hit` is
  false) in `absent | engine_key | doc_hash | unsolved | display_quality | budget |
  decode_error`, plus `components_changed` for assemblies, and `cache` diagnostics:
  parts `{entries:{<name>: present|absent|skipped:<limit>}, checkpoints:{restored,
  checkpoints, matched_features, features, full_result, full_result_reason?, reason?},
  hydrated_from?: cache.ckpt|cache.brep, hydrate_error?, features_executed}`; assemblies
  `{skipped?:{<name>: <limit>}}`. `budget` covers both a reader-limit skip and a writer omission.
- `doc_save` → `cache: {written (cache.json written), entries:[names written],
  skipped_reason? (feature_failed|empty_mesh|budget|not_converged|component_failed|error),
  failed_features?, failed_components?, error?, dropped?:[{entry, reason, …}],
  checkpoints:{written, bytes, checkpoints_total, checkpoints_written, checkpoints_dropped,
  full_result, omitted_after_failure?, reason? (feature_failed|partial_build|budget|
  no_features|too_many_features|not_built_this_session|no_checkpoints|inc_rebuild_disabled)}}`.

`CHUDWARE_INC_REBUILD=0` disables writing and restoring `cache.ckpt` with the rest of the
incremental-rebuild cache. Relocation (`native_file_operations`) invalidates `cache.ckpt`
with the other geometry caches.

## 10. Planar outward-normal cache epoch (2026-10-09)

The current semantic key is `chudware-geomcache/8` plus the OCCT version. Epoch 8 takes
every planar outward normal from the frame-handedness-aware helper
(`core/geom/face_normal.h`): a left-handed plane frame (revolve end/shoulder faces) used to
read inward, so holes, weld plates and text placed on such faces now build differently.
Older caches are a cache miss (`engine_key`) and rebuild from the native source.

## 11. Shell validation cache epoch (2026-10-09)

The current semantic key is `chudware-geomcache/9` plus the OCCT version. Epoch 9 makes the
shell feature accept an offset result only when it is BRepCheck-valid, watertight and moved
the volume the right way, trying every join/tolerance strategy before refusing, and resolves
each geometry-query element of a face-list slot (`shell.remove`, `draft.faces`, …) like the
bare query. Shells on freeform bodies and multi-face query lists now build differently.
Older caches are a cache miss (`engine_key`) and rebuild from the native source.

## 12. Geometric `near` cache epoch (2026-10-09)

The current semantic key is `chudware-geomcache/10` plus the OCCT version. Epoch 10 ranks a
geometry query's `near` point by its distance to each candidate's trimmed geometry (or its
centroid, whichever is smaller) instead of centroid distance alone (`contracts/selectors.md`
§"near: geometric distance"). A saved `near` query whose point lies on a large entity whose
centroid is far away (a flange rim) can now resolve to a different edge/face than epoch 9
did, so dress-ups referencing it build differently. Older caches are a cache miss
(`engine_key`) and rebuild from the native source.

## 13. Sectional shell cache epoch (2026-10-10)

The current semantic key is `chudware-geomcache/11` plus the OCCT version. Epoch 11 gives the
shell feature a fallback for inward shells the offset ladder refuses: a body whose opened
faces are parallel planar ends is hollowed by lofting its exact eroded cross-sections and
cutting that cavity (`core/geom/shell_sections.cpp`), validated to a wall within 2% of the
thickness. A rectangle-to-circle loft (BRepOffset_MixedConnectivity) used to fail the shell
feature and now builds. Older caches are a cache miss (`engine_key`) and rebuild from the
native source.

## 14. Strict profile region pick cache epoch (2026-10-10)

The current semantic key is `chudware-geomcache/12` plus the OCCT version. Epoch 12 applies
`contracts/selectors.md` §8: a profile region pick never moves silently to another region.
Before this epoch, a sketch edit that removed regions let a cut lose regions with `ok:true`.
A cache built that way shows the damaged geometry. Thus older caches are a cache miss
(`engine_key`). The part rebuilds from the native source, and a bad pick fails by name.

## 15. AutoCAD/ACIS import cache epoch (2026-10-11)

The current semantic key is `chudware-geomcache/13` plus the OCCT version. Epoch 13 imports the
ACIS 3DSOLID/BODY/REGION/SURFACE entities of DXF/DWG files (and `.sat`/`.sab`) as exact bodies,
and an embedded DXF/DWG/SAT import keeps one body per baked compound child (`embedded_bodies`), so
a drawing profile with construction wires stays one body. Older caches lack those solids and are a
cache miss (`engine_key`); the import rebuilds from its embedded geometry.

## 16. Joining fillet cache epoch (2026-10-12)

The current semantic key is `chudware-geomcache/15` plus the OCCT version. Epoch 15 fillets an
edge shared by two bodies by filling its concave corners and joining the bodies into one solid
(`contracts/doc_schema.md` §"Fillet on an edge shared by two bodies"). Before this epoch the
same feature failed as "too large". Older caches are a cache miss (`engine_key`).

## 17. Build-state write-back on rebuild (2026-10-12)

Additive; no stored format changes and the engine key is unchanged. A build is saved into the
file it was built from, not only by `doc_save`, so a stale cache (an engine epoch bump, a
`document.json` edited outside Chudware, a missing or unsolved cache) costs one rebuild, not one
per open:

- **`doc_open`** of a native `.step+` (not `detach_path`) that missed its cache writes the build
  it just produced back into that file: a part when the open executed features that succeeded
  (a display-tier re-mesh or a retry of the same failing features writes nothing); an
  assembly whenever its saved display missed. The response reports it in
  `cache.writeback = {written, reason?, entries, dropped?, checkpoints?, cache_skipped_reason?,
  components?}`; `components` lists `{id, build_state}` for every component the rebuild compiled.
- **Assembly component loads** of a self-contained part (no legacy file-backed import) whose
  `cache.json`/`cache.brep` did not serve compile it as a part session does and write the same
  full build state into the component file (`AsmComponentStatus.build_state`).
- Only the build-state members (`cache.json`, `cache.aicm`, `cache.brep`, `cache.ckpt`,
  `geometry.step`) are replaced. `document.json`, `meta.json`, `thumbnail.png`, drawings, studies
  and unknown entries are copied as their original compressed streams, byte for byte, so the
  vault (`services/plm/snapshot.is_derived_entry`), AI-VC (`authored_sha256`) and every other
  content comparison see an unchanged document. Budget and STORED rules are §4's.
- The write refuses, leaving the file untouched, unless the file still carries the size+mtime
  stamp taken before the read and its `document.json` still hashes to the built `doc_hash`
  (`file_changed`, `document_changed`); a save that lands while the copy is written wins.
  Unwritable files report `io_error: …`. A refusal never fails the open or the load.
- Assembly `components` fingerprint rows for native files now also carry `doc_hash`; a row
  whose size/mtime changed still matches when both rows carry the same `doc_hash`, so a
  component's build-state write-back does not invalidate the assemblies that use it.

## 18. Non-mutating unify cache epoch (2026-10-12)

The current semantic key is `chudware-geomcache/16` plus the OCCT version. Epoch 16 runs
UnifySameDomain on a topology copy of each boolean result. OCCT's unify edits the edges of the
faces it merges in place, and a NonDestructive boolean shares untouched TShapes with its
argument, which is a cached compile checkpoint. Before this epoch, a cut followed by an undo or
delete could restore a corrupted, BRepCheck-invalid prefix, and `doc_save` persisted that state
into `cache.ckpt`. A subtractive feature whose exact volume grows now fails by name
(`… produced a body LARGER than its target`). Older caches are a cache miss (`engine_key`) and
rebuild through §17's write-back.

## 19. Cache provenance and rooted native references (2026-10-13)

Derived build state carries `cache.provenance`, a JSON envelope with schema
`chudware-cache-provenance/1`, `manifest`, and lowercase hexadecimal `mac`.
The manifest is canonical compact JSON (lexicographically sorted object keys):
`{engine, doc_hash, members}`, where `doc_hash` is SHA-256 of the canonical authored
document (the existing noncryptographic cache-header identity remains unchanged), and
`members` maps every written `cache.json`, `cache.aicm`, `cache.brep`, and `cache.ckpt`
member to its SHA-256 digest. `mac` is HMAC-SHA256 over the canonical manifest.
Optional-member admission happens before signing; omitted members are not claimed.
Readers authenticate the whole signed manifest and independently verify the members
they admit and consume. Skipped or absent unrelated members do not invalidate admitted
checkpoints or component BREP state.
`cache.provenance` is derived state, replaced by build-state write-back and invalidated
on relocation, never copied as an authored attachment.

The 32-byte random signing key is `cache-provenance.key` in the cell's `--cell-state`
directory or the desktop's Chudware data directory. It is atomically published,
owner-private (`0600` on POSIX), and never read from a document or workspace. Cell
startup supplies trusted `CHUDWARE_CELL_STATE` and `CHUDWARE_NATIVE_ROOT` before loading
the engine; tenant workers do not receive the state key.

A matching MAC reports `cache_trust:"verified"` and retains warm open/activation:
zero features execute. An absent, foreign, or invalid MAC does **not** block opening
otherwise usable cached geometry: it reports `cache_trust:"unverified"` and
`cache_verification_pending:true`, displays and hydrates the saved state immediately,
and re-solves the feature history in a background scratch session with no restored
checkpoints or unverified component-cache reuse. Completion replaces derived geometry
without editing the feature history or revision, reports `cache_verification` with
`completed`, `mismatch`, and guarded `writeback`, warns `cache_mismatch` when the display
differs, and writes fresh authenticated state into the unchanged source file. Edits
arriving during verification are retained; verification restarts against their current
tree. Failed verification remains unverified and reports `cache_verification_failed`.
The asynchronous refresh sink publishes the replacement outside document locks.
Assembly component BREP loads follow the same trust policy; a containing session's
background rebuild bypasses unverified components and updates their persisted state.

With an allowed root configured (cell: `/work`), document-embedded component/import
references, pump-curve file references, and numerical system-export destinations must
be relative, contain neither NUL nor parent traversal, and resolve beneath that root
without traversing symlinks (including dangling links). Unsafe references are refused
before cached-state replay, sink access, or committing a native editing batch: rejected
insert/edit/paste operations leave the tree and revision unchanged. Inline pump CSV is
classified before filesystem access. SVG sheet IDs
and output-name components are always safe leaf identifiers, independently of root
configuration; traversal, absolute paths, separators and NUL are refused.
Import provenance is stored workspace-relative even when the native RPC has already
mapped its input to an absolute OS path; legacy file-backed imports resolve that
reference against the configured workspace root. Assembly component references resolve
against the containing document/project folder (or workspace root for unsaved cells).
Rooted save-as refuses references that would require an absolute/traversing spelling.


## 20. Sketcher overhaul cache epoch (2026-10-16)

The current semantic key is `chudware-geomcache/17` plus the OCCT version. Epoch 17 projects an
oblique straight model edge as one analytic line (it used to be a sampled run of collinear
segments), expands composite rectangles/polygons/polylines with their relations, and solves the
extended 2D relation set (junction tangency, normalized collinear/parallel residuals, no
wrong-type residuals). Converted entities and solved sketch coordinates of older caches can
differ, so they are a cache miss (`engine_key`) and rebuild through §17's write-back.
