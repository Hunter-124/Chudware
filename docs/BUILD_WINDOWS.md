# Building AI-CAD on Windows

Status: the **C++ core (with and without the OCCT geometry kernel)** and the **Python runtime +
test suite** are verified on Windows 11 (MSVC 14.44 / VS 2022 Build Tools, CMake 4.3, Ninja 1.13,
Python 3.12). The legacy `host/` app ships on Windows today; the cross-platform `ui/imgui` app
now has a `windows-app` preset but is not yet human-verified on Windows (T-WIN1 — see [ImGui app](#imgui-app-uiimgui--the-cross-platform-gui-t-win1-in-progress)).

## 0. One-time checkout fix — symlinks

This repo uses git symlinks for short top-level names (`engine`→`engine_oracle`,
`agent`→`services/agent`, `corpus`→`corpora/sketch`, …). A default Windows checkout has
`core.symlinks=false`, so these land as **13-byte text files**, which breaks Python imports,
`python -m engine.x` subprocesses, and data-file access under `corpus/` etc.

Fix (pick one):

```powershell
# Recommended: convert them to directory junctions (no admin needed). Idempotent.
powershell -File scripts\setup_windows_links.ps1
```

or enable real symlinks before cloning: `git config --global core.symlinks true` **and** turn on
Windows **Developer Mode**, then re-clone.

## 1. Toolchain

- **Visual Studio 2022 Build Tools** with the *Desktop development with C++* workload
  (MSVC + Windows SDK). Install elevated:
  ```powershell
  winget install Microsoft.VisualStudio.2022.BuildTools --override "--add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --quiet"
  ```
- **CMake** and **Ninja** (user scope, no admin):
  ```powershell
  winget install Kitware.CMake Ninja-build.Ninja
  ```

## 2. Build the C++ core

MSVC's `cl`/`link` are only on PATH inside a developer environment. Use the **x64 Native Tools
Command Prompt for VS 2022** (Start menu), or run `vcvars64.bat` first:

```bat
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
cd /d <repo>
cmake --preset windows-core
cmake --build --preset windows-core
ctest --test-dir build/windows-core --output-on-failure
```

Expected: clean build; **18/20 ctest pass**. Two known pre-existing failures (not Windows-build
issues): `cam` writes to a hardcoded `/tmp` path, and `freecad_parity` has an MSVC-vs-GCC solver
convergence difference.

Windows portability notes baked into the build:
- `core/cam` gets `_USE_MATH_DEFINES` (MSVC hides `M_PI` behind it; GCC/Clang don't).
- `ui/abstract` links `nlohmann_json` explicitly (no ambient system include on Windows).
- `test_cam` uses `temp_directory_path()` instead of a hardcoded `/tmp`.

## 2b. Build the core WITH the OCCT geometry kernel

```bat
cmake --preset windows-occt
cmake --build --preset windows-occt
ctest --test-dir build/windows-occt --output-on-failure
```

The **first** configure downloads OCCT 7.9.3 (~250 MB) and builds Netgen once (cached under
`third_party/`, a few minutes). Verified on Win11 + MSVC 14.44: **clean build, 48/49 ctest**
(only `freecad_parity` fails — the same MSVC-vs-GCC solver-convergence diff as the OCCT-free build).

Windows-specific fixes this required:
- `core/CMakeLists.txt` defines `NOMINMAX` for MSVC — `<windows.h>` (pulled in by OCCT headers)
  otherwise defines `min`/`max` macros that clobber `std::min`/`std::max`.
- `ProvisionNetgen.cmake` picks the install target by generator (Ninja uses `install`, not the
  Visual Studio `INSTALL`).

## 3. Python runtime + tests

Every dependency ships a prebuilt `cp312` wheel — **no compiler needed** for the Python side.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt pytest pytest-timeout
# run tests via the short alias dirs (junctions from step 0), e.g.:
.\.venv\Scripts\python -m pytest engine_oracle -q --timeout=90
```

Expected: ~997/1002 `engine_oracle` tests pass. Remaining failures are pre-existing/cross-platform
(a missing `board` fixture in `ecad`, a `docs/contracts/fixtures` path bug, one numerical LBM test)
plus a missing `langgraph-checkpoint-sqlite` dependency that blocks `services/dag` collection.

## Troubleshooting / gotchas

- **NEVER `git stash` in this repo on Windows.** The step-0 compat symlinks get materialized as
  absolute symlinks; a `git stash push`/`pop` cycle follows them and **deletes the real
  target-directory contents** (~784 files across `services/`, `engine_oracle/`, `ui/imgui/`).
  Recovery is `git reset --hard HEAD` (history is never affected). To land a commit despite the
  symlink "changes", `git add` only your edited file(s), then `git pull --rebase` / `git push` —
  don't stash. Running `scripts/setup_windows_links.ps1` (junctions + skip-worktree) avoids the
  dirty-symlink state entirely.
- **`cl` / `cmake` "is not recognized"** — MSVC isn't on `PATH` in a normal shell. Build from the
  **x64 Native Tools Command Prompt for VS 2022** (Start menu) or run `vcvars64.bat` first.
- **Known ctest failure** `freecad_parity` (both `windows-core` 19/20 and `windows-occt` 48/49) is a
  pre-existing MSVC-vs-GCC solver-convergence diff, not a build problem — don't chase it as a Windows
  regression.
- **Legacy `UnicodeEncodeError` on `work.py board`** was a cp1252-console bug, fixed on `master`
  (`work.py` forces UTF-8 stdout). If you hit it on an old checkout, pull latest.

## ImGui app (`ui/imgui`) — the cross-platform GUI (T-WIN1, in progress)

`ui/imgui/` (target `AI-CAD2`) is the **single cross-platform GUI** that replaces the legacy
`host/` app on both Linux and Windows. Its `CMakeLists.txt` already carries the Windows link
block (`opengl32`/`dwmapi`/`comdlg32`), so the code is ready; T-WIN1 is about wiring + verifying
the Windows build. A `windows-app` preset now exists as the starting point:

```
cmake --preset windows-occt      REM build libaicad.dll first (the app dlopens it at runtime)
cmake --build --preset windows-occt
cmake --preset windows-app       REM configure the ImGui GUI (build\windows-app\)
cmake --build --preset windows-app
build\windows-app\bin\AI-CAD2.exe
```

**STATUS: NOT yet human-verified on Windows.** Likely first snags to work through: the
`AICAD_DEFAULT_LIBAICAD` compile-time default path (points at the Linux `.so` — needs the Windows
`.dll` path or a runtime override), and staging the OCCT/Netgen sibling DLLs next to `AI-CAD2.exe`
(reuse `build.bat`'s dual-layout staging logic). Once this builds + renders with UI parity,
Windows can drop the legacy `host/` app (below) and `host/` is deleted (T-G11) — collapsing the
two GUI trees into one. Please report findings so the preset/docs can be corrected.

## Desktop app (`host/`) — LEGACY Windows app (deleted by T-G11)

> Legacy tree. New GUI work goes in `ui/imgui/` (above), never here — see AGENTS.md. This
> section documents the app Windows ships **today**, until T-WIN1 lands `windows-app`.

The imgui desktop host **builds, launches, and renders on Windows 10/11** (MSVC 14.44,
VS 2022). Build it with:

```
build.bat            REM configures host\ (VS 2022 gen), compiles AI-CAD.exe, stages dist\AI-CAD\
```

`build.bat` stages `libaicad.dll` + the OCCT/Netgen sibling DLLs next to the exe (it now
finds them in **either** the VS `core\build-occt\bin\Release` layout **or** the Ninja
`build\windows-occt\bin` preset layout — see the dual-layout staging fix).

**Headless screenshot capture** (drives the GUI with no visible window):

```
set AI_CAD_HIDDEN=1
set AI_CAD_REPO=C:\path\to\CAD
set AI_CAD_SHOT=sketch      REM scenarios: settings|chat|ribbon|pmform|m1|sketch|asm|fea|...
dist\AI-CAD\AI-CAD.exe      REM writes PNGs to %AI_CAD_REPO%\scratch\<scenario>_shots\
```

Verified via the `sketch` scenario: UI chrome (ribbon, FeatureManager, Copilot, dark theme)
renders correctly; the sketch solver reports **Fully Defined DOF: 0**; and the engine builds
a real parametric solid (`Sketch1 -> Boss-Extrude1 -> Solid Bodies(1)/Body1`). So the
**geometry kernel, sketch constraint solver, and UI all reach parity on Windows.**

### Open parity gaps (T-WIN1, honest status)

- **Viewport geometry framing in headless capture is unconfirmed.** In `sketch`-scenario
  captures the viewport *background* renders (sketch plane, origin triad) but the sketch
  profile / extruded solid are not visibly framed. Undetermined whether the geometry is
  drawn-but-not-zoomed-to-fit vs not drawn under `AI_CAD_HIDDEN`. **Needs an interactive
  (visible-window) eyeball on Windows to confirm final render parity vs Linux.**
- **`AI_CAD_SHOT=m1` scenario hangs headless.** On Windows `AppState::tryStartWorker()` requires
  the managed **Python worker** (the in-process `libaicad` fallback is `#ifndef _WIN32`,
  Linux-only — `host/src/core/app_state.cpp`); if worker start / the venv-freshness re-provision
  gate stalls, the scenario never fires. The app is console-subsystem but its diagnostics go to
  the in-app log (not stdout), so headless runs are hard to observe. Filed for the app-gui/engine
  owner; not a WIN-lane fix.
- **`freecad_parity` ctest** (48/49) — pre-existing MSVC-vs-GCC solver-convergence diff on the
  point-on-ellipse case; reproduced here, not a Windows regression (see above).
