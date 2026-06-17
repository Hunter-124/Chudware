# C++ Core Migration — the strangler plan (host integration → zero Python)

**Goal (locked with the user 2026-06-16):** drive the project to **zero Python**.
The pure-C++ core (`core/`) replaces the Python `engine/` (and eventually `agent/`,
`mcp_server/`, ECAD, sim, formats, tooling) one subsystem at a time, with no
regression at any step. End state: host + engine are one process, the C++ core
attached as **`libaicad.dll`** (a *separate* DLL, the user's choice, so OCCT stays
out of the host exe and the link is reversible during migration).

This file is the **living source of truth** for the migration: the seam, the
per-step plan, and the feature parity gap list. Update it as each step lands.

## The seam

Every host→engine call funnels through ONE dispatch point:
`WorkerClient::call(method, params, result, err, timeout, onEvent)` in
[`host/src/worker_client.cpp:160`](../host/src/worker_client.cpp). Today it writes
line-delimited JSON-RPC to a Python `engine.worker` subprocess.

The C++ core mirrors that exact shape via a flat C ABI in
[`core/api/libaicad.h`](../core/api/libaicad.h):

```c
int  aicad_call(const char* method, const char* params_json, char** out_json); // 0 = handled
void aicad_free(char* s);
const char* aicad_abi_version(void);
```

`aicad_call` returns **0** when the C++ core handled the method, **nonzero** (with
an `{"error":...}` payload) when it did not — which is the host's signal to **fall
back to the Python worker** for that method. This is the strangler valve: as each
method reaches parity, the host routes it to `libaicad` and the Python path for it
is deleted.

## Steps

- **Step 1 — additive link (DONE).** `libaicad.dll` builds from `core/` (SHARED
  target `aicad`, links `aicad_doc` → the whole OCCT geom kernel transitively;
  OCCT runtime DLLs staged next to it by `aicad_occt_copy_dlls`). `aicad_probe`
  loads it via `LoadLibrary` exactly as the host will and round-trips a real box
  compile → AICM bytes. ctest `probe` green. **No host change yet, fully reversible.**

- **Step 2 — host loads libaicad + diagnostic RPC (DONE).**
  `host/src/core/cpp_core.{h,cpp}` `LoadLibrary`s the DLL (compile-only in the host
  build — no OCCT link). A diagnostic compile runs the C++ core in-process on demand
  and returns AICM + statuses, leaving the **production Python path untouched**.
  Behind `AI_CAD_CPP_PARITY=1`, after each successful `applyOps` + the `activate`
  (doc_rebuild) refresh, the host also runs the C++ compile on the same tree and
  logs a `[cpp-parity]` line. Verified live (`AI_CAD_SHOT=m1`).

- **Step 2.5 — schema convergence (IN PROGRESS).** The host sends the CANONICAL
  feature shape (`{id,type,params:{...},refs:{slot:<sel|datum>}}`, engine/doc/
  schema.py); the early C++ compiler read a SIMPLIFIED flat shape. `compile_document`
  now `normalize_feature()`s both into the flat form, translating the two structural
  divergences (sketch plane datum `plane.top`→`top`; region-selector profile list
  → sketch id). **sketch → extrude on canonical input now matches Python**
  (`tools/cpp_parity.py`: box/cylinder/washer 3/3). REMAINING canonical mapping:
  extrude `mode` add/cut (needs a target-body boolean), revolve axis refs, boolean
  `targets`/`tools` (vs simplified a/b), canonical fillet/chamfer/hole/pattern/
  mirror selector refs. Each lands as the matching feature is ported.

- **Step 3+ — cut over per feature, delete Python per wave.** A method/feature is
  cut over only once its C++ implementation passes the parity dashboard for the
  relevant corpus. The corresponding Python is deleted in the same commit. Track
  progress in the gap list below.

## Parity dashboard

`tools/cpp_parity.py` (run under the venv) compiles a fixture set through BOTH the
Python oracle (`engine.doc.compiler.compile_document`) and the C++ core
(`libaicad.dll` via ctypes), and diffs per-feature ok + body count. Exit code =
number of non-matching fixtures (0 = full parity), so it doubles as a CI gate.

```
& "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" tools\cpp_parity.py
```

Current: **24/24** — the full common solid-modeling feature set on canonical host
input, gated on exact BRep volume (see the DONE table below).

## Feature parity gap list (C++ core vs Python `engine/doc`)

`compile_document` (the C++ feature compiler) currently implements a **simplified
subset** with **id-string refs** (not the full selector objects the host sends for
Instant3D drag / assembly). Cutover order is roughly Tier A → B; ECAD / sim / agent
(Tiers B/C) are later epics.

**Solid-modeling features: DONE — 24/24 geometric parity** (`tools/cpp_parity.py`,
exact BRep-volume gate, on canonical host input):

| Feature / subsystem        | Python                                  | C++ core            | Status |
|----------------------------|------------------------------------------|---------------------|--------|
| primitives box/cyl/sphere/cone/torus | `features/primitives.py`        | `geom/features.cpp` | DONE (centered; `at` placement deferred) |
| sketch → extrude (boss/cut)| `features/extrude.py`                     | `geom/features.cpp` | DONE (blind/through_all/mid_plane; thin/taper/dir-2 deferred) |
| sketch → revolve           | `features/revolve.py`                     | `geom/features.cpp` | DONE (angle/axis datum; thin deferred) |
| sweep                      | `features/sweep.py`                       | `geom/features.cpp` | DONE (MakePipe path; guides/twist/thin deferred) |
| loft                       | `features/loft.py`                        | `geom/features.cpp` | DONE (ThruSections; guides/centerline deferred) |
| rib                        | `features/rib.py`                         | `geom/features.cpp` | DONE (parallel/centered; normal-mode/draft deferred) |
| boolean fuse/cut/common    | `features/boolean.py`                     | `geom/booleans.cpp` | DONE |
| fillet / chamfer           | `features/dress.py`                       | `geom/dress.cpp` + `doc/picks.cpp` | DONE (edge selector) |
| shell                      | `features/dress.py` (shell)               | `geom/dress.cpp`    | DONE (remove-faces; thickness overrides deferred) |
| draft                      | `features/draft.py`                       | `geom/dress.cpp`    | DONE (neutral-plane mode; parting_line/step deferred) |
| hole (simple/cbore/csink)  | `features/hole.py`                        | `geom/hole.cpp`     | DONE (no threads/grid/sketch-positions) |
| linear / circular pattern  | `features/pattern.py`                     | `geom/transform.cpp`| DONE (geometry-pattern; cut-seed op deferred) |
| mirror                     | `features/pattern.py` mirror              | `geom/transform.cpp`| DONE (datum/face plane) |
| datum plane (offset)       | `features/datum.py`                       | `doc/document.cpp`  | DONE (offset method; 3-pt/angled deferred) |
| selectors (B-rep refs)     | `selectors.py`                            | `doc/selectors.cpp` | DONE (resolve §3; `resolve_region` deferred) |
| tessellate                 | `doc/tessellate.py`                       | `geom/tessellate.cpp` | DONE |

**Remaining (the larger epics — not yet started):**

| Feature / subsystem        | Python                                  | Status |
|----------------------------|------------------------------------------|--------|
| **doc session / ops / undo** | `doc/session.py`, `doc/ops.py`        | STARTED — `core/doc/session.{h,cpp}` ports DocSession + the core op vocabulary; `libaicad` serves `doc_new`/`doc_apply_ops`/`doc_undo`/`doc_redo`/`doc_tree`/`doc_rebuild`/`doc_close`. `tools/cpp_session_parity.py` 6/6 (add/edit/undo/redo/stale_revision/box+fillet) vs Python session. **owner_map** (host picking) is ported in `compile_document` (TShape-identity diff per body vs its derivation source): exact for additive/single-feature parts. **P14 (DONE):** the dress ops (fillet/chamfer/shell/draft) now set `source_of`, so the identity diff keeps the UNCHANGED faces with the original feature and gives the modified+new ones to the dress feature — matching the Python build123d attribution EXACTLY (a fillet owns every face it regenerated, not just the new round; the earlier coarse behavior attributed the whole body to the op because `source_of` was unset for fillet/chamfer). Verdict: `cpp_session_parity.py` box+fillet owner per-feature counts are gated STRICTLY (f2=2 faces/7 edges, f3=5 faces/8 edges, matching the oracle). The exact `(body, topo_index)` tuples still differ only by OCCT-vs-build123d `MapShapes` enumeration order (the same flagged artifact as edge resolve_pick). Booleans (multi-source) remain coarse — a separate, harder owner-attribution problem. **`doc_resolve_pick`** (pick → re-resolvable selector) ported (`make_pick_selector` + `resolve_pick`): face selectors match the Python oracle exactly (centroid/axis/surf/measure/owner); edge selectors carry the same explorer-vs-MapShapes quirk as Python and differ only by OCCT-vs-build123d enumeration order. **P12 — `.step+` `doc_save`/`doc_open` (DONE):** `core/doc/stepplus.{h,cpp}` (miniz ZIP read+write) writes the container (`thumbnail.png` STORED placeholder, `meta.json`, `document.json` + preserved unknown entries; atomic `.tmp`→rename) and reads it back (container-version validated, unknown entries carried for resave). The parametric round-trip is JSON-only/faithful (`compile_document` is deterministic); `geometry.step` (the frozen-rebuild interchange/fallback via OCCT `STEPControl_Writer`) + drawing persistence are deferred. **P13 — advanced sketch ops (DONE):** `core/doc/sketch_geom.{h,cpp}` ports the 2D curve math (intersect/split/extend/offset-chain/fillet/chamfer) of `engine/doc/sketch_geom.py`, and `session.cpp` ports the eight `engine/doc/ops.py` handlers `sketch_trim` / `sketch_extend` / `sketch_offset` / `sketch_fillet` / `sketch_chamfer` / `sketch_mirror_entities` / `sketch_pattern_entities` / `sketch_move` (entity splice + dangling-constraint drop + new coincident/tangent/symmetric/equal relations + the `$N`/`$N#k` id_map scheme). `sketch_convert` (Convert Entities) is DEFERRED — it needs the `sketch_project_ref` model-topology projection. `cpp_session_parity.py` 18/18 (10 advanced-op cases, each an EXACT structural match of entities + constraints + id_map vs the Python session). **P15 — resulting-document validation (DONE):** `core/doc/validate.{h,cpp}` is a faithful port of `engine/doc/schema.py`'s part-document path (validate_part + all sub-validators: common/appearances/equations/rollback/configurations/feature/sketch_params/entity/constraint/dim/selector/handle + a self-contained non-evaluating expression-grammar validator + the eq-curve grammar + all the ENTITY_FIELDS/CONSTRAINT_SPECS/FEATURE_TYPES tables). `session.cpp doc_apply_ops` now runs it on the post-batch working doc and throws `invalid_op` exactly like the Python `apply_ops` (so a malformed batch is rejected, not silently committed). Diagnostic `doc_validate` RPC + `tools/cpp_validate_parity.py` 41/41 (valid baseline + 6 valid variants + 34 targeted invalid mutations, accept/reject verdict matched against the oracle). **Remaining:** sketch_convert, assembly ops, configurations editing, copy/paste/derived, modify-op owner attribution, rollback-bar compile, then the host routing switch. The C++ session deliberately does NOT reproduce the Python session's compile-cache region quirk (editing a downstream depth leaves the profile dangling) — it always recompiles, which is the SolidWorks-correct behavior. |
| **sheet metal**            | `features/sheetmetal*.py`                 | TODO (epic) |
| **surfacing (12 ops)**     | `features/surf_*.py`                       | TODO (epic) |
| **datums / curves (rest)** | `features/datum.py`, `curves.py`          | TODO (axis/point/csys/3-pt plane, projected curves) |
| **split / import / weldment** | `features/{split,import_feature,weldment}.py` | TODO |
| **equations.evaluate (full)** | `doc/{equations,evaluate}.py`          | partial (`util/expr.cpp`) |
| **drawing / dxf / pdf / stepplus** | `doc/{drawing,dxf,pdfvec,stepplus}.py` | TODO |
| **formats (STEP/IGES/STL/…)** | `doc/formats.py`                       | TODO |
| **ECAD (full)**            | `engine/ecad/*`                           | TODO (epic) |
| **sim (FEA/CFD/SPICE)**    | `engine/sim/*`                            | TODO (epic; CUDA) |
| **agent / MCP / providers**| `agent/*`, `mcp_server/*`                 | TODO (epic; needs C++ LLM client) |

**Cutover note:** the C++ core now MATCHES on the solid-feature recompile, but
production routing still requires the doc session/ops/undo layer (so `doc_apply_ops`
can be served in-process). That layer is the next cutover prerequisite; until then
the strangler runs in diagnostic/parity mode.

## Build / test quick ref

```
cmake -S core -B core/build-occt -DAICAD_WITH_OCCT=ON
cmake --build core/build-occt --config Release
ctest --test-dir core/build-occt -C Release --output-on-failure   # incl. `probe`
```

`libaicad.dll` + its OCCT DLLs land in `core/build-occt/bin/Release/`.
