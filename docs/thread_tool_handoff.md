# Handoff — Thread tool fixes + modeling-core perf

You are continuing work on the **Thread feature** of AI-CAD (a C++/OCCT parametric CAD app).
Work directly on **master**, commit + push each coherent increment with **explicit pathspecs**
(the tree often carries unrelated WIP). End commit messages with
`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## Original request (5 asks)
1. **Perf**: applying the Thread tool and changing its length regenerates the preview with
   "insane lag."
2. **SAE default**: default thread sizing to SAE sizes when the doc is in **IPS** mode.
3. **Auto-size from face**: picking a common bolt shaft (e.g. a 5/16 cylinder) should auto-apply
   the common thread size/pitch (5/16 → **5/16-18** as the default option).
4. **Start reference + flip**: the thread length needs a "from" reference (edge/point/surface) to
   start from, plus a button to flip the run direction when the reference isn't already an
   auto-termination end.
5. **Full-span default**: a thread should NOT auto-pick a size/length; by default it spans the
   whole selected cylindrical face.

## DONE + committed on master
- **Engine (asks #4, #5)** — `core/doc/document.cpp` thread block (`type == "thread"`, ~L6411)
  and `core/doc/validate.cpp` (L166):
  - `length` is now optional; blank ⇒ full face axial span.
  - new `from`/`start_ref` ref (vertex/edge/face/datum) → thread start plane, clamped to face.
  - new `flip`/`reverse` bool → run toward the other end.
  - All resolved in the doc compiler; `geom::cut_thread` signature UNCHANGED (it already clamps the
    band to the body). Do NOT touch the fragile `helical_bool` internals in `core/geom/thread.cpp`
    (TRUE-FRENET sweep, fuzzy ladder, trim-before-boolean are hard-won bug fixes).
  - Commit `256029e3`.
- **PM form (asks #4, #5 UI)** — `host/src/ui/pm_forms.cpp` thread form (~L379):
  - "Full length" check (default on) omits Length → full-span; uncheck reveals Length.
  - "Start From" well `K_FACEEDGEVERTEX`, "Flip direction" check.
  - Commit `83b450a1`.
- **Git**: landed all branch WIP, merged `feat/cef-headless-daemon` → master, deleted that branch.

## Perf diagnosis (profiler-driven — this is the key result; trust these numbers)
Measured with a standalone harness driving the real `aicad_call` C API + `perf` (NOT the MCP
engine, which is confounded by multiple open docs + autosave serialize storms giving bogus
45–160s numbers). Test part: `Thread-tool-test.step+` (ø14 × 57mm shaft), thread its cylindrical face.

| Path | Cost | Verdict |
|---|---|---|
| Fixed per-op overhead (serialize/validate/persist), cheap box edit | **0.1 ms** | no systemic tax |
| Tessellation of dense threaded body | **74 ms** | not a problem |
| `cut_thread` boolean, well-matched size (M14×2) | **~1–3 s** | **intrinsic OCCT `BRepAlgoAPI_Cut`** |
| `cut_thread`, MISMATCHED size (M3 on ø14) | **~10 s** | fuzzy-ladder retries a failed BOP; fixed by good defaults |

**Conclusion:** there is NO hidden systemic bug. "Slowness all over the modeling core" = any op that
triggers a heavy OCCT boolean costs 1–3 s, and the GUI re-runs that on every live-preview edit.
`perf` shows `helical_bool`/`BRepAlgoAPI_Cut` + its TBB workers dominate (~50%+ inclusive). The
fix is **architectural, not algorithmic**: heavy features must show a CHEAP preview during drag and
compute real geometry only on commit.

Reproduce/profile (harness sources in the session scratchpad, or rebuild — see recipe below).

## DONE 2026-07-10 (commit bfa4974d) — all three items below shipped, awaiting live red-pen
- **#2 SAE default**: IPS docs open the thread form with Size = 1/4-20 UNC.
- **#3 auto-size**: picking a cylindrical face snaps Size via the selector's kernel-computed
  `radius` (GSL Stage 0) — NO engine RPC needed. Shaft matches major Ø, bore matches basic minor.
- **#1 draft preview**: PM sends `params.preview=true` during live edits; the engine thread
  compiler renders a plain turned band (measured 18–50 ms vs 4.4 s real full-span). commit()
  re-applies without the flag (committing_), so OK always produces real geometry. The old
  length-shorten proxy was removed (the full-length default bypassed it anyway).
- Deployed to services/dist (temp+rename). Verify in the GUI: drag length/flip → band updates
  live; OK → real thread after the one-time boolean.

## Original remaining-work notes (historical)
1. **#2 SAE default in IPS** — in `host/src/ui/property_manager.cpp` `initField`/`beginXxx`
   (`ips_` = `st.ips()` is available, ~L481/L694). When the thread form opens and `ips_` is true,
   default the `size` combo to an SAE size (e.g. `1/4-20`); metric otherwise. Low risk.
2. **#3 auto-size from picked face** — hook the well-pick acceptance in `property_manager.cpp`
   (~L1022, `addFacePick`-style). When the active form is `thread`, the well is `target_face`, and
   the pick is a cylindrical face: resolve the cylinder radius (needs an engine RPC — e.g.
   `query_geometry`/`hover` on the selector) and set `size` to the nearest standard major-Ø within a
   tolerance band (a 5/16 shaft is turned slightly undersize: 0.3125" ≈ 7.94 mm → `5/16-18`). Pick
   SAE vs metric by `ips_`. **GOTCHA**: no synchronous `worker.call` in draw paths — do the RPC in
   the event/pick path, back off on busy (see memory `gl-thread-rpc-freeze`). The size tables already
   exist in `core/doc/document.cpp` (`kMetric`, `kUnified`, ~L6441) and `core/parts/catalog.cpp`.
3. **#1 the headline: cosmetic live-preview for heavy features** — currently
   `host/src/ui/property_manager.cpp` sends the SAME real-geometry op for both live preview and
   commit (preview = `doc_apply_ops` edit_feature; OK keeps the last preview batch; Cancel undoes).
   So every drag pays the 1–3 s boolean. Plan: add a `preview:true`/`draft` flag on the op during
   drag; the thread compiler honors it by producing a CHEAP representation (best = a display-only
   cosmetic thread: shade the cylindrical band + helix-line overlay, NO boolean, ~ms — SolidWorks
   style; the current `mode:cosmetic` path at `document.cpp:6566` returns the body UNCHANGED with no
   visual, so it needs a real cheap visual). On OK, re-apply WITHOUT the flag to get real geometry.
   Confirm the preview↔commit wiring so commit doesn't persist the cheap preview.

## Build / deploy / profile recipes
- **Core lib**: `ninja -C core/build-occt-linux libaicad.so` (Release -O3). `document.cpp` is in
  `aicad_doc`; thread geom is in `aicad_geom_occt` (NOT `aicad_geom` — that archive is stale).
- **Deploy to the MCP engine**: `./build.sh stage` — stages libaicad.so + its Netgen/lib3mf
  siblings AND aicad-hostd from `core/build-occt-linux/bin` into `services/dist/AI-CAD/` via
  copy-to-temp + atomic rename (memory: NEVER `cp` over a live `.so` → SIGBUS; the script does
  the temp+rename for you). The running MCP engine only reloads on respawn (doc_id counter
  resets to 1); a running hostd daemon self-drains on the mtime flip.
- **Host GUI**: `ninja -C host/build-linux CMakeFiles/AI-CAD.dir/src/ui/pm_forms.cpp.o` to
  compile-check one TU; `ninja -C host/build-linux AI-CAD` for the full app.
- **Clean perf harness** (bypasses server/autosave/multi-doc confound — the ONLY reliable way to
  measure per-op cost): a `tt2.cpp` that links `core/build-occt-linux/bin/libaicad.so` and calls
  `aicad_call("doc_open", {"path":...})` then `aicad_call("doc_apply_ops", {"doc_id","base_revision":-1,"ops":[...]})`.
  Build: `g++ -O2 -std=c++17 tt2.cpp -o tt2 core/build-occt-linux/bin/libaicad.so
  -Wl,-rpath,$PWD/core/build-occt-linux/bin`. Profile: `perf record -g -F 999 -- ./tt2; perf report`.
  A cut_thread-only micro-harness links `core/geom/thread.cpp` +
  `core/build-occt-linux/libaicad_geom_occt.a libaicad_geom.a libaicad_util.a libaicad_clipper2.a` +
  `_deps/*.a` + system `-lTK*`, includes `-I/usr/include/opencascade -I/usr/include/eigen3`.
  `AI_CAD_THREAD_DEBUG=1` enables the `[thread]` trace in `thread.cpp`.

## Gotchas
- `validate.cpp` mirrors the compiler's required-params — if you make a param optional in
  `document.cpp`, update `validate.cpp` too or the resulting doc fails validation.
- The MCP `add_features` auto-backgrounds >45 s and its timing is confounded — do NOT use it to
  benchmark; use the harness.
- Repo has a graphify knowledge graph: prefer `graphify query "..."` before broad grep.
- `graphify update .` after code changes.
