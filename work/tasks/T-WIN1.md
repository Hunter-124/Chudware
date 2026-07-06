---
id: T-WIN1
title: Windows 10 full-app bring-up (OCCT core + imgui GUI) under MSVC
workstream: WIN
owner: gissa
status: doing
branch: task/T-WIN1-windows-bringup
depends_on: []
lane: platform-infra
kind: new
priority: 2
---

## Context

The OCCT-free C++ core already compiles under MSVC (windows-core preset;
18/20 ctest — see memory windows-cpp-core-build-verified) and the host/ GUI has
been shown to build+run on Win11 after a fix_math.h guard + build.bat staging
fix. This task takes the FULL app the rest of the way on Windows 10 / MSVC:
full OCCT core, the ui/imgui GUI, app launch, a part actually rendering, then the
AI_CAD_SHOT headless capture battery with an honest eyeballed parity report vs
Linux.

This task file is being PROPOSED by the gissa session because the WIN workstream
was assigned (T-WIN1) but no board entry existed. It is drafted for foreman/human
review + merge; the atomic claim happens only after it lands on master's board.

Known landmine (already addressed on this branch, keep in mind for regressions):
the previous MSVC failure came from how host/extern/fix_math.h is force-included
(/FI). Commit 750c0bbb on this branch guards those C99 float decls behind
`_MSC_VER >= 1950` (VS2026 UCRT) so older MSVC (14.44) and Linux are no-ops,
avoiding C2733 on nexttowardf. Any new math-related MSVC break: check that wiring
FIRST. Every fix must be a platform seam — the Linux build stays byte-for-byte
untouched.

## Files / areas touched (WIN seams only)

- `host/extern/fix_math.h` — MSVC /FI float-decl guard (done, 750c0bbb)
- `build.bat` — dual-layout core-DLL staging: VS `core\build-occt\bin\Release`
  AND Ninja `build\windows-occt\bin` preset layout (done, 750c0bbb)
- `CMakePresets.json` — windows-occt / windows-core presets (extend if OCCT full
  core needs additional Windows-only flags)
- `docs/BUILD_WINDOWS.md` — bring-up steps + Troubleshooting/gotchas
- `ui/` / imgui GUI build glue — Windows-only compile/link seams as they surface
- NO edits to services/agent, services/mcp, services/browser, contracts/, or any
  tools/translator or T-W*/T-X* owned path (other active sessions).

## Steps / stages

1. Full OCCT core build under MSVC via the windows-occt preset
   (`cmake --build --preset windows-occt`). Capture the exact first error if it
   breaks; math errors -> check fix_math.h /FI wiring before anything else.
2. Stage libaicad.dll + OCCT/Netgen sibling DLLs next to the exe (build.bat path
   already handles both layouts) and confirm the host loads the in-process C++
   engine (no AI_CAD_LIBAICAD override).
3. Build the ui/imgui GUI on MSVC; fix Windows-only compile/link seams (guarded).
4. Launch the app; confirm it opens and can create geometry (doc/sketch/extrude
   ops backed by the C++ core, not the Python worker).
5. Render a part in-app; eyeball it against the Linux render.
6. Run the AI_CAD_SHOT headless capture battery; produce an honest, eyeballed
   parity report vs Linux (documented broken stage + exact error is a GOOD result;
   "mostly builds" is NOT).
7. Docs: fold every real gotcha into docs/BUILD_WINDOWS.md.

## Definition of done

- [ ] Full OCCT core builds under MSVC (windows-occt preset); ctest result recorded
- [ ] Core DLLs stage next to the exe; host loads the in-process C++ engine
- [ ] ui/imgui GUI builds and the app launches on Windows 10
- [ ] A part renders in-app
- [ ] AI_CAD_SHOT headless battery runs; eyeballed Linux-parity report attached to
      the progress log (per-stage PASS / documented-break with exact error)
- [ ] Every Windows fix is a platform seam; Linux build verified untouched
- [ ] docs/BUILD_WINDOWS.md updated with the real bring-up path + gotchas

## AI hints

- Memories: windows-cpp-core-build-verified, windows-desktop-gui-build-works,
  windows-symlink-imports-broken, windows-git-stash-symlink-landmine (NEVER
  `git stash` in this repo on Windows), windows-board-cp1252-fix.
- `python3` is not on PATH on this Windows box; use `python` (3.12.10).
- Robocopy staging in build.bat uses /R:2 /W:1 — a locked DLL means a running
  app/MCP is holding libaicad.dll / OCCT TK*.dll; close them and rerun.

## Reviewer summary (current state — 2026-07-05)

Cold-read snapshot for a reviewer who wasn't following live; the dated Progress log below
has the full trail.

VERIFIED on Windows (Win11, MSVC 14.44 / VS2022):
- Full OCCT core builds (windows-occt preset); ctest **48/49** — the 1 fail is
  `freecad_parity` (point-on-ellipse), a pre-existing MSVC-vs-GCC solver diff, NOT a
  Windows regression.
- Core DLLs stage next to the exe; host imgui GUI builds + launches + renders.
- Engine + sketch solver + UI reach parity: a real parametric solid builds
  (Sketch1 → Boss-Extrude1 → Body1); sketch solver hits "Fully Defined DOF: 0".

OPEN — headless viewport parity screenshot is UNVERIFIED. Root-caused to a camera-framing
bug (NOT a render gap) and fixed on this branch (commit 82c785bf), but the fix is unverified
because re-capture is blocked by the standing worker-start hang (below). Closes via a 2-min
interactive eyeball (launch AI-CAD.exe → create a part → press F to Zoom-to-Fit) OR the
worker-start fix.

FIX ON THIS BRANCH FOR REVIEW — commit 82c785bf, `host/src/ui/scenarios.cpp` sketch step 2:
the shot reset yaw/pitch/zoom but not `st.pan`; entering the sketch leaves pan offset, so the
solid framed off-centre. Fix resets `st.pan[0..2]=0` before capture, mirroring
`ViewPreset::ZoomFit`'s `kZeroPan`. Capture-only, Linux-safe, no engine/worker change; normal
interactive launch path is UNCHANGED. Status: UNVERIFIED (see OPEN above).

STANDING BLOCKER (buddy / app-gui territory — NOT a WIN-lane fix; do not touch without sign-off):
`AppState::tryStartWorker()` gates the in-process libaicad fallback `#ifndef _WIN32`
(`host/src/core/app_state.cpp:580`) = Linux-only, so on Windows the app requires the managed
Python worker; when worker-start / the venv-freshness re-provision stalls, headless scenarios
hang with NO stdout diagnostic (errors go to the in-app log only). **Observed twice:**
(1) `AI_CAD_SHOT=m1`, (2) `AI_CAD_SHOT=sketch` re-capture — both recorded here for whoever
picks up the Windows worker-start fix.

## Progress log
- 2026-07-05 [gissa/opus-wynn] Branch task/T-WIN1-win10-bringup created off master
  c745f0fe. Preserved prior verified WIN work as commit 750c0bbb (fix_math.h /FI
  MSVC 14.51 guard + build.bat dual-layout core staging) — both platform-guarded,
  Linux untouched. Task file drafted here for foreman review because no board entry
  for T-WIN1 existed. NOT self-claimed / not merged — awaiting foreman to land this
  on master's board, then gissa claims atomically and proceeds with stages 1-7.
- 2026-07-05 [gissa/opus-wynn] Verification pass on this Win11 box (MSVC 14.44, VS2022).
  STAGES 1-3 VERIFIED:
  * Stage 1 — full OCCT core (windows-occt preset) already built + current; `ctest`
    = **48/49** (mesher_autopick PASSES at 159s — earlier "timeout" was only a 120s
    ctest limit; the one real fail is `freecad_parity` point-on-ellipse solver
    non-convergence, which matches the documented Windows baseline, pre-existing).
  * Stage 2 — libaicad.dll + OCCT TK*.dll stage into dist\AI-CAD\ next to the exe (✓).
  * Stage 3 — host imgui GUI (AI-CAD.exe) builds via build.bat + stages (✓).
  STAGE 4-6 PARTIAL, honest findings:
  * The GUI renders fully on Windows (verified from headless `AI_CAD_SHOT=sketch`
    captures in scratch/sketch_shots): ribbon, FeatureManager, Copilot, dark theme all
    correct; sketch solver = "Fully Defined DOF: 0"; engine created a real parametric
    solid (Sketch1 -> Boss-Extrude1 -> Solid Bodies(1)/Body1). => kernel + solver + UI
    reach parity.
  * OPEN: viewport GEOMETRY framing in headless capture is blank/unframed (background/
    plane/triad render, but the profile/solid aren't visibly framed) — needs an
    INTERACTIVE visible-window eyeball on Windows to confirm final render parity. This
    is the right spot for gissa (human at the GUI) to verify.
  * BLOCKER: `AI_CAD_SHOT=m1` hangs headless — Windows `tryStartWorker()` needs the
    Python worker (in-process libaicad fallback is `#ifndef _WIN32`, Linux-only,
    host/src/core/app_state.cpp:580); worker-start / venv-freshness re-provision stalls
    with no stdout diagnostic (errors go to the in-app log). Flagged for app-gui/engine
    owner; NOT a WIN-lane fix (deliberate "Python worker is the Windows production path"
    design per that file's comment).
  DOCS: docs/BUILD_WINDOWS.md updated — replaced the stale "Not yet verified: ImGui
  desktop app" section with the verified build+headless-capture flow and the three
  honest open parity gaps above.
  NET: full app builds + launches + models on Windows; the remaining honest gap is
  interactive viewport render-parity confirmation + the m1 headless worker hang.
- 2026-07-05 [gissa/opus-wynn] Viewport-framing investigation (gissa directed: force
  zoom-to-fit + re-capture). ROOT CAUSE FOUND (not a render gap): the sketch scenario's
  solid step sets st.yaw/pitch/zoom but NOT st.pan — entering the sketch animates a
  Normal-To that leaves st.pan offset, so zoom=1 frames the scene radius but OFF-CENTRE
  and the solid falls outside the shot. The renderer itself is fine (sketch plane +
  triad + full UI all render). FIX (host/src/ui/scenarios.cpp, sketch step 2): reset
  st.pan[0..2]=0 before capture, mirroring ViewPreset::ZoomFit's kZeroPan. Capture-only,
  platform-agnostic (correct on Linux too), does NOT touch engine/worker routing.
  DEVIATION FROM NORMAL LAUNCH PATH: none at runtime — the fix is in the scenario code,
  not an env/flag; normal interactive launch is unchanged.
  STATUS: UNVERIFIED. Rebuilt host (build.bat host, exit 0) + re-ran AI_CAD_SHOT=sketch
  headless (AI_CAD_HIDDEN=1, AI_CAD_PYTHON=venv) but it produced NO fresh capture in 78s
  — the app hung on worker-start AGAIN (same tryStartWorker `#ifndef _WIN32` blocker as
  m1; the prior 05:34 sketch_shots were from a session where the worker DID start). So
  the framing fix is correct-by-inspection but I could not produce a fresh framed
  screenshot headlessly. Per gissa's 1-2-iteration cap: STOPPED here rather than guessing
  further. TO CLOSE PARITY, either: (a) buddy's call on the tryStartWorker Windows path
  so headless capture works, or (b) gissa does the 2-min interactive launch (worker
  starts fine interactively) + press F (Zoom to Fit) + eyeball the solid.
