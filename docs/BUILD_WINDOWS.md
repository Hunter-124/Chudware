# Building AI-CAD on Windows

Status: the **C++ core (with and without the OCCT geometry kernel)**, the **Python runtime +
test suite**, and the **unified `ui/imgui` GUI (`windows-app` / `AI-CAD2.exe`)** are all verified
on Windows 11 (MSVC 14.44 / VS 2022 Build Tools, CMake 4.3, Ninja 1.13, Python 3.12). The legacy
`host/` app tree was **deleted** (T-G11); `ui/imgui` is now the **single** cross-platform GUI on
both Linux and Windows — see [ImGui app](#imgui-app-uiimgui--the-cross-platform-gui-t-win1-verified).

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

## ImGui app (`ui/imgui`) — the cross-platform GUI (T-WIN1, VERIFIED)

`ui/imgui/` (target `AI-CAD2.exe`, aliased `ShopCraft.exe`) is the **single cross-platform GUI**
on both Linux and Windows. The legacy `host/` tree was **deleted** (T-G11) — there is no
Windows-only GUI. Build it after the core:

```
cmake --preset windows-occt      REM build libaicad.dll first (the app loads it at runtime)
cmake --build --preset windows-occt
cmake --preset windows-app       REM configure the ImGui GUI (build\windows-app\)
cmake --build --preset windows-app
build\windows-app\bin\AI-CAD2.exe
```

**STATUS: VERIFIED on Win11 — MSVC 14.44 / VS 2022 Build Tools, CMake 4.3, Ninja (T-WIN1).**
Clean `windows-app` build; `AI-CAD2.exe` links and renders the full GUI backed by the in-process
C++ OCCT engine (no Python worker required for modeling). Smoke tests, both green:

```
build\windows-app\bin\AI-CAD2.exe --selftest
REM -> [selftest] save=1 open=1 features=3 rev=0 -> OK   (part built + .step+ round-trips via libaicad)

build\windows-app\bin\AI-CAD2.exe --shot out.png --frames 30
REM -> writes a 1616x1039 PNG: ribbon + icons + fonts, FeatureManager (Base/Fillet/Shell),
REM    dark theme, and the 3D viewport solid FRAMED correctly — full UI + render parity with Linux.
```

The framed-viewport shot closes the viewport-framing question left open in the old `host/`-era
notes: the unified app draws **and** frames geometry correctly on Windows.

### Windows seams fixed for the bring-up

All are `if(WIN32)`/`if(MSVC)` build-file conditionals in `ui/imgui/CMakeLists.txt` — the Linux
`else()` path is byte-for-byte unchanged (no source forks, no `host/` resurrection):

- **`/FI fix_math.h` quoting (fatal C1083).** The MSVC force-include shim was written
  `/FI"...path..."`; under the **Ninja + MSVC** generator a repo path containing a space (e.g.
  `AI PROJECTS`) leaks the literal quote chars into the filename, so `cl` reports *"Cannot open
  include file"*. Fix: drop the inner quotes (`/FI${path}`) so Ninja quotes the whole `/FI<path>`
  token, which `cl` unwraps. (The old `host/` VS-generator build never exercised this quoting.)
- **Engine resolution.** `AICAD_DEFAULT_LIBAICAD` — the runtime fallback `ui/imgui/src/main.cpp`
  drops into the environment when `AI_CAD_LIBAICAD` is unset and no `libaicad.dll` sits next to
  the exe — pointed at the dead VS layout `core/build-occt/bin/Release/`. Repointed at the Ninja
  preset output `build/windows-occt/bin/libaicad.dll`; `cpp_core`'s `SetDllDirectoryW` then puts
  that directory on the process search path so the ~100 sibling OCCT/Netgen `TK*.dll` **transitive**
  deps also load — no manual DLL staging needed. To use a different build, set
  `AI_CAD_LIBAICAD=<absolute path to libaicad.dll>`.
- **Stale glad message** corrected — glad lives at `extern/glad` (`GLAD_DIR`), not `host/extern/glad`.

`freecad_parity` (windows-occt 48/49) remains the one pre-existing MSVC-vs-GCC solver-convergence
diff on the point-on-ellipse case — not a Windows regression, not chased.
