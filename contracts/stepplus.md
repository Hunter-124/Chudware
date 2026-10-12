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
- Atomic save: write to `<name>.step+.tmp`, then replace (no torn files on crash).

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
