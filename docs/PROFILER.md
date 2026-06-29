# Sampling profiler — finding modeling performance hotspots

A true **in-process sampling profiler** built into `libaicad.{dll,so}`. It periodically captures
the call stack of the thread doing modeling work and aggregates the samples, so the hot
frames rise to the top statistically — telling you *where inside* a slow op (extrude,
fillet, boolean, pattern, tessellation, rebuild) the time actually goes, down into OCCT.

It is scoped to **modeling** by default (the `doc_*` RPCs); simulation/ECAD are not sampled
unless you ask for `mode: "all"`.

> Source: `core/prof/sampler.{h,cpp}` (the sampler), `core/api/libaicad.cpp` (RPCs +
> auto-scoping + env-var), `tools/profile_modeling.py` (headless driver),
> `tools/flamegraph.py` (folded → SVG), `host/src/ui/profiler_window.{h,cpp}` (in-app window).
> Unit test: `core/tests/test_prof.cpp` (`ctest -R prof`).

## How it works (and why it's safe)

On **Windows**, a dedicated sampler thread wakes every interval, **suspends** the registered
work thread, walks its stack **lock-free** (`RtlLookupFunctionEntry` + `RtlVirtualUnwind`
reading the module's memory-mapped `.pdata` — no heap, no DbgHelp), then resumes it. This avoids
the classic in-process-profiler deadlock where you suspend a thread mid-`malloc` (holding the
heap lock) and then try to allocate. Raw return addresses are symbolized **later**, in the
report, via DbgHelp — when nothing is suspended.

On **Linux**, a dedicated sampler thread periodically signals the registered work thread
(`tgkill` with a real-time signal); an **async-signal-safe** handler captures that thread's
stack with `backtrace()` into a lock-free ring (no `malloc`/locks in the handler — the libgcc
unwinder is warmed up at `start()`). The sampler thread drains the ring into the same
aggregation. Raw addresses are symbolized **later**, in the report, via `dladdr()` +
`abi::__cxa_demangle`.

Both backends feed ONE platform-neutral aggregation + report core, so the report JSON is
identical. `libaicad` ships a PDB on Windows (`/Zi /DEBUG`, optimization kept via `/OPT:REF,ICF`)
and exports its dynamic symbols on Linux (`-Wl,--export-dynamic`) so OUR frames resolve to real
names. OCCT's exported symbols resolve too (e.g. `TKMath!math_BFGS::Perform`); anything without
symbols shows as `module!0x<rva>`. On Linux `backtrace()` unwinds from inside the signal handler,
so a couple of profiler frames (the handler + signal trampoline) ride at the tip of each stack —
harmless noise the flame graph shows as a thin constant cap.

## Four ways to use it

### 1. Headless repro driver (best for reproducible hunting)

```
python tools/profile_modeling.py                       # "all" workload, 5 s
python tools/profile_modeling.py --workload boolean --seconds 8 --hz 2000
python tools/profile_modeling.py --list                # available workloads
python tools/profile_modeling.py --workload fillet --out runs/fillet
```

Workloads: `fillet`, `chamfer`, `shell`, `boolean`, `pattern`, `all`. Each builds a fresh
document and applies the ops every iteration (so the kernel really recompiles), prints a
module + hottest-frame table, and writes `<out>.folded`, `<out>.speedscope.json`,
`<out>.svg`.

### 2. Live app / MCP toggle (profile a real session)

The sampler is in the DLL the app loads, so you can profile actual interactive modeling.
Call the RPCs (e.g. via the `ai-cad` MCP, or any `aicad_call` caller):

| RPC | params | does |
|-----|--------|------|
| `prof_start` | `{hz?:1000, interval_us?, mode?:"modeling"\|"all"}` | begin sampling |
| `prof_status` | — | `{active, samples, interval_us, mode}` |
| `prof_report` | `{top?, write_folded?, write_speedscope?, min_samples?}` | report **without** stopping |
| `prof_stop` | same as report | stop + return the report |

Typical: `prof_start` → do the slow thing in the app → `prof_stop {write_folded, write_speedscope}`
→ render with `python tools/flamegraph.py the.folded -o the.svg` (or drop the file into
https://speedscope.app).

### 3. Env-var auto-profile (zero-touch, whole session)

```
set AI_CAD_PROFILE=1
set AI_CAD_PROFILE_HZ=1000        # optional
set AI_CAD_PROFILE_OUT=C:\path    # optional; default %APPDATA%\AI-CAD\prof
```

Launch the app (or MCP) with `AI_CAD_PROFILE=1`, model normally, close it — a report
(`modeling.folded` + `modeling.speedscope.json`) is written on exit. Good for catching a
slow op during ordinary use without instrumenting anything.

### 4. In-app Profiler window (interactive, in the desktop app)

Open the **Profiler** window in the app (Evaluate menu) — it drives the same `prof_start` /
`prof_stop` / `prof_report` RPCs and renders the result inline: the module self-time table, the
flat self/total tables, and an **interactive flame graph** parsed from the folded stacks (widest
box = hottest, left-aligned; hover any box for its frame + sample count). Click **Start**, model,
then **Stop** (or **Refresh** to peek without stopping). No files needed — everything renders from
the report returned over the in-process RPC seam.

## Reading the output

- **Module table (self-time)** is the fastest read — which DLL the CPU is actually in:
  - `libaicad` — our dispatch, sketch constraint solver, document compile glue
  - `TKMath` — numerical core (BFGS optimizer, polynomials) — heavy intersection/projection math
  - `TKGeomAlgo` / `TKGeomBase` / `TKG3d` / `TKG2d` — curve/surface evaluation + intersection
  - `TKBO` / `TKBool` — booleans (fuse/cut/common) — hole drilling, pattern merges
  - `TKFillet` — fillet / chamfer blends
  - `TKPrim` — primitives + prisms (extrude)
  - `TKMesh` — tessellation (the render/display mesh)
  - `TKShHealing` — shape healing/fixing
  - `jemalloc` / `tbb12` — OCCT's allocator + thread pool (high % = allocation churn)
- **Flat self-time** — the hottest individual functions (where the CPU is).
- **Flat total** — functions most often *on* the stack (the callers responsible).
- **Flame graph** (`.svg`) — widest box = hottest; hover for sample counts. Read top-down to
  see which op a hot leaf belongs to.

## Caveats

- **Windows + Linux.** Windows uses `SuspendThread`/`RtlVirtualUnwind`/DbgHelp; Linux uses a
  periodic signal + `backtrace()` + `dladdr`/`__cxa_demangle`. On any other platform the profiler
  compiles to no-ops and `report()` returns an "unsupported" envelope.
- Modeling runs single-threaded under the global doc lock, so sampling one thread captures
  everything — there is no hidden parallel modeling work to miss.
- After rebuilding `libaicad`, **restart the app + MCP** so they load the new library (they hold
  the old one open). On Windows the build stages the DLL/PDB into `dist/AI-CAD/` rename-aside;
  delete the `libaicad.old*.dll` files after the restart.

## Building

```
cmake --build core/build-occt --config Release --target aicad
```

If the link of the huge `document.cpp`/`libaicad.cpp` TUs hits
`D8040: error creating or communicating with child process` or a `Permission denied` /
`LNK1136 corrupt` on an `.obj`, that's the known compiler-child contention — kill orphaned
`cl`/`mspdbsrv`/`MSBuild`, delete the offending `.obj`, and rebuild single-threaded with
`-- /m:1 /p:CL_MPCount=1`.
