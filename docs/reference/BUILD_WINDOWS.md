# Building Chudware on Windows

Status: the **C++ core (with and without the OCCT geometry kernel)** and the **Python runtime +
test suite** are verified on Windows 11 (MSVC 14.44 / VS 2022 Build Tools, CMake 4.3, Ninja 1.13,
Python 3.12). The GUI on both Linux and Windows is the **`host/` tree** (binary `Chudware`,
`Chudware` compat symlink) — the interim `ui/imgui` shell is retired and deleted. Re-verifying the
restored `host/` build under MSVC is an open item (see `docs/plans/GUI_OVERHAUL_SPEC.md`).

## 0. Checkout portability

All Python packages and fixture corpora are referenced by their canonical
`services/...` and `corpora/...` paths. The repository has no committed symlinks,
so a default unelevated Windows checkout needs no Developer Mode, junction setup,
or `core.symlinks` override.

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
- The core and host targets link `nlohmann_json` explicitly (no ambient system include on Windows).
- `test_cam` uses `temp_directory_path()` instead of a hardcoded `/tmp`.

## 2b. Build the core WITH the OCCT geometry kernel

```bat
cmake --preset windows-occt
cmake --build --preset windows-occt
ctest --test-dir build/windows-occt --output-on-failure
```

The first configure downloads OCCT 7.9.3 and the pinned BSD-3-Clause
Polyhedral-FEA source under `third_party/`.

That build also produces the Polyhedral-FEA driver `polymesh.exe` in the preset's `bin\`
directory, beside `chudware.dll` and `chudware-hostd.exe` (see
`docs/reference/HOSTD.md`). To build against a local Polyhedral-FEA checkout instead of the
pin, add `-DPOLYMESH_SOURCE_DIR=<checkout>` to the configure line (the `build.sh`
`CHUDWARE_POLYMESH_ROOT` env var is the POSIX front end for the same variable); the
configure then prints `Polyhedral-FEA: LOCAL SOURCE <dir> @ <sha>` instead of
`Polyhedral-FEA: PINNED FETCH …`.

Windows-specific fixes this requires:
- `core/CMakeLists.txt` defines `NOMINMAX` for MSVC because OCCT pulls in
  `<windows.h>`, whose macros otherwise clobber `std::min`/`std::max`.

## 3. Python runtime + tests

Every dependency ships a prebuilt `cp312` wheel — **no compiler needed** for the Python side.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt pytest pytest-timeout
# services-side Python suites (the C++ engine is exercised via ctest on the core build):
.\.venv\Scripts\python -X utf8 -m pytest services/agent -q --timeout=90
```

The legacy `engine_oracle` Python suite is gone (zero-Python cutover, T-RF21) — engine
correctness proof on Windows is `ctest` on the `windows-core` / `windows-occt` core builds.

## Troubleshooting / gotchas

- **`cl` / `cmake` "is not recognized"** — MSVC isn't on `PATH` in a normal shell. Build from the
  **x64 Native Tools Command Prompt for VS 2022** (Start menu) or run `vcvars64.bat` first.
- **Known ctest failure** `freecad_parity` (both `windows-core` 19/20 and `windows-occt` 48/49) is a
  pre-existing MSVC-vs-GCC solver-convergence diff, not a build problem — don't chase it as a Windows
  regression.
- **Legacy `UnicodeEncodeError` on `work.py board`** was a cp1252-console bug, fixed on `master`
  (`work.py` forces UTF-8 stdout). If you hit it on an old checkout, pull latest.

## GUI app (`host/`) — Windows build

The single cross-platform GUI is `host/` (target `Chudware`, output binary
`chudware.exe`). The self-hosted Windows runner builds the OCCT-free core and
restored `host/` tree for `v*` release tags (and manual dispatches) through
`.github/workflows/native-build.yml`, runs both CTest trees, then launches the
GUI in `CHUDWARE_SHOT=chat` mode and verifies a real PNG capture. That smoke
forces the managed worker to fail and points `CHUDWARE_LIBCHUDWARE` at the DLL
built by the same job, so it also proves the Windows in-process C++ fallback
rather than inheriting a pre-provisioned Python runtime from the persistent
runner. Full release packaging still uses the `windows-occt` preset.

- **`/FI fix_math.h` quoting (fatal C1083).** Under the **Ninja + MSVC** generator a repo path
  containing a space leaks literal quote chars into the force-include filename. Fix: drop the
  inner quotes (`/FI${path}`) so Ninja quotes the whole `/FI<path>` token, which `cl` unwraps.
- **Engine resolution.** `CHUDWARE_DEFAULT_LIBCHUDWARE` is the runtime fallback when
  `CHUDWARE_LIBCHUDWARE` is unset and no `libchudware.dll` sits next to the exe; it must point at the
  Ninja preset output `build/windows-occt/bin/libchudware.dll`. `cpp_core` adds that
  directory to the DLL search path so OCCT and the optional advisor runtime load without
  manual staging. To use a different build, set
  `CHUDWARE_LIBCHUDWARE=<absolute path to libchudware.dll>`.
- **Worker fallback.** `AppState::tryStartWorker()` tries the managed worker
  first on every desktop platform. If that is unavailable, Windows now follows
  the same `cpp_core().ensureLoaded()` fallback as Linux/macOS; failed runtime
  provisioning no longer prevents a headless capture or a core-backed desktop
  session.
- **glad** lives at `extern/glad` (`GLAD_DIR`).

`freecad_parity` (windows-occt 48/49) remains the one pre-existing MSVC-vs-GCC solver-convergence
diff on the point-on-ellipse case — not a Windows regression, not chased.
