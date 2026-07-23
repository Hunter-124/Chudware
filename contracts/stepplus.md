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

| entry | doc kind | contents |
|---|---|---|
| `cache.json` | part + asm | header `{engine, doc_hash, solved: true, response}`; for assemblies also `components`: fingerprints `[{path, size, mtime}]` of every non-suppressed component's resolved source file — reopen replays the cache only if ALL still match |
| `cache.aicm` | part + asm | the saved tessellation (AICM blob), STORED uncompressed |
| `cache.brep` | part | compiled bodies as `"AICBREP1"` magic · u64 count · per-body u64 length + BinTools stream (**no triangulation** — BinTools-restored triangulation triggers runaway BRepMesh refinement on NURBS faces, OCCT 7.9); lets an assembly load this part as a component without re-running `compile_document` |
