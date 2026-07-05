---
id: T-WIN1
title: Windows 10 full-app bring-up (OCCT core + imgui GUI) under MSVC
workstream: WIN
owner: gissa
status: todo
branch: task/T-WIN1-win10-bringup
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

## Progress log
- 2026-07-05 [gissa/opus-wynn] Branch task/T-WIN1-win10-bringup created off master
  c745f0fe. Preserved prior verified WIN work as commit 750c0bbb (fix_math.h /FI
  MSVC 14.51 guard + build.bat dual-layout core staging) — both platform-guarded,
  Linux untouched. Task file drafted here for foreman review because no board entry
  for T-WIN1 existed. NOT self-claimed / not merged — awaiting foreman to land this
  on master's board, then gissa claims atomically and proceeds with stages 1-7.
