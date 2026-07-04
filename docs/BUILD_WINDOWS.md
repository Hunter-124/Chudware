# Building AI-CAD on Windows

Status: the **OCCT-free C++ core** and the **Python runtime + test suite** are verified on
Windows 11 (MSVC 14.44 / VS 2022 Build Tools, CMake 4.3, Ninja 1.13, Python 3.12). The full
OCCT geometry kernel and the ImGui desktop app are not yet wired for Windows (see
[Not yet](#not-yet-verified)).

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

## Not yet verified

- **OCCT geometry kernel** (`AICAD_WITH_OCCT=ON`) — `cmake/ProvisionOCCT.cmake` auto-downloads
  OCCT; the Windows path is untested.
- **ImGui desktop app** (`AICAD_BUILD_APP=ON`) — still references `host/extern/glad`; blocked on the
  `host/` deletion + a Windows app preset.
