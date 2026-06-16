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

- **Step 2 — host loads libaicad + diagnostic RPC (DONE/IN PROGRESS).**
  `host/src/core/cpp_core.{h,cpp}` `LoadLibrary`s the DLL (compile-only in the host
  build — no OCCT link). A diagnostic `doc_compile_cpp` runs the C++ core
  in-process on demand and returns AICM + statuses, leaving the **production Python
  path untouched**. Behind `AI_CAD_CPP_PARITY=1`, after each Python `doc_rebuild`
  the host also runs the C++ compile on the same tree and logs a parity line.
  `tools/cpp_parity.py` builds a Python-vs-C++ dashboard over saved part trees.

- **Step 3+ — cut over per feature, delete Python per wave.** A method/feature is
  cut over only once its C++ implementation passes the parity dashboard for the
  relevant corpus. The corresponding Python is deleted in the same commit. Track
  progress in the gap list below.

## Feature parity gap list (C++ core vs Python `engine/doc`)

`compile_document` (the C++ feature compiler) currently implements a **simplified
subset** with **id-string refs** (not the full selector objects the host sends for
Instant3D drag / assembly). Cutover order is roughly Tier A → B; ECAD / sim / agent
(Tiers B/C) are later epics.

| Feature / subsystem        | Python                                  | C++ core            | Status |
|----------------------------|------------------------------------------|---------------------|--------|
| sketch → extrude           | `features/extrude.py`                     | `geom/features.cpp` | ported (subset: depth/mid-plane; no start/end modes, thin, taper, dir-2) |
| sketch → revolve           | `features/revolve.py`                     | `geom/features.cpp` | ported (subset: angle; no thin) |
| boolean fuse/cut/common    | `features/boolean.py`                     | `geom/booleans.cpp` | ported |
| fillet / chamfer           | `features/dress.py`                       | `geom/dress.cpp` + `doc/picks.cpp` | ported (edge selector + dir filter) |
| linear / circular pattern  | `features/pattern.py`                     | `geom/transform.cpp`| ported (geometry-pattern, id-ref) |
| mirror                     | `features/pattern.py` / mirror            | `geom/transform.cpp`| ported |
| hole (simple/cbore/csink)  | `features/hole.py`                        | `geom/hole.cpp`     | ported (subset: no thread tables / cosmetic threads / grid / sketch positions) |
| selectors (B-rep refs)     | `selectors.py`                            | `doc/selectors.cpp` | ported (resolve §3; `resolve_region` NOT ported) |
| **loft**                   | `features/loft.py`                        | —                   | TODO |
| **sweep**                  | `features/sweep.py`                       | —                   | TODO |
| **rib**                    | `features/rib.py`                         | —                   | TODO |
| **draft**                  | `features/draft.py`                       | —                   | TODO |
| **shell / thicken**        | `features/surf_thicken.py` etc.           | —                   | TODO |
| **sheet metal**            | `features/sheetmetal*.py`                 | —                   | TODO |
| **surfacing (12 ops)**     | `features/surf_*.py`                       | —                   | TODO |
| **datums / curves**        | `features/datum.py`, `curves.py`          | —                   | TODO |
| **primitives**             | `features/primitives.py`                  | partial (box)       | TODO |
| **split / import / weldment** | `features/{split,import_feature,weldment}.py` | —             | TODO |
| **doc session / ops / undo** | `doc/session.py`, `document.py`         | —                   | TODO (needed before `doc_apply_ops`/`doc_new` cut over) |
| **equations.evaluate (full)** | `doc/{equations,evaluate}.py`          | `util/expr.cpp` (subset) | partial |
| **tessellate**             | `doc/tessellate.py`                       | `geom/tessellate.cpp` | ported |
| **drawing / dxf / pdf / stepplus** | `doc/{drawing,dxf,pdfvec,stepplus}.py` | —              | TODO |
| **formats (STEP/IGES/STL/…)** | `doc/formats.py`                       | —                   | TODO |
| **ECAD (full)**            | `engine/ecad/*`                           | —                   | TODO (epic) |
| **sim (FEA/CFD/SPICE)**    | `engine/sim/*`                            | —                   | TODO (epic; CUDA) |
| **agent / MCP / providers**| `agent/*`, `mcp_server/*`                 | —                   | TODO (epic; needs C++ LLM client) |

## Build / test quick ref

```
cmake -S core -B core/build-occt -DAICAD_WITH_OCCT=ON
cmake --build core/build-occt --config Release
ctest --test-dir core/build-occt -C Release --output-on-failure   # incl. `probe`
```

`libaicad.dll` + its OCCT DLLs land in `core/build-occt/bin/Release/`.
