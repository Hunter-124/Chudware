# Building Chudware on Windows

The `windows-core` (OCCT off) and `windows-occt` (OCCT + Polyhedral-FEA) presets configure
MSVC + Ninja builds in `CMakePresets.json`. The Windows CI row
(`.github/workflows/native-build.yml`, GitHub-hosted) builds `windows-occt` plus the
`host/` GUI and runs both CTest trees.
The GUI on every desktop platform is the **`host/` tree** (CMake target `Chudware`,
output binary `chudware.exe`).
Pass/fail state is whatever the latest CI run reports — this page records no pass counts.

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
- **CMake ≥ 3.24** (the presets' minimum) and **Ninja** (user scope, no admin):
  ```powershell
  winget install Kitware.CMake Ninja-build.Ninja
  ```

## 2. Build trees

Every preset configures the repository root (which adds `core/` as a subdirectory) into
`build/<preset>/`:

| Tree | What lives there |
|------|------------------|
| `build/<preset>/bin/` | engine artifacts: `libchudware.dll`, `chudware-hostd.exe`, `polymesh.exe`, test executables |
| `build/<preset>/core/` | the core's registered CTest suite — run `ctest` here, not on `build/<preset>` |
| `host\build-windows` | host GUI tree used by `native-build.yml` |
| `host\build-win` | host GUI tree used by `scripts/build_windows.ps1`, `release.yml` and `services/installer/build_release.py` (packaging reads `host\build-win\bin\chudware.exe`) |

`libchudware.dll` and `chudware-hostd.exe` are only built when OCCT is on
(`CHUDWARE_WITH_OCCT`), so `windows-core` is a fast core/test build, not a runnable engine
for the GUI. Use `windows-occt` for anything the GUI or packaging consumes.

## 3. Build the C++ core

MSVC's `cl`/`link` are only on PATH inside a developer environment. Use the **x64 Native Tools
Command Prompt for VS 2022** (Start menu), or run `vcvars64.bat` first:

```bat
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
cd /d <repo>
cmake --preset windows-core
cmake --build --preset windows-core
ctest --test-dir build/windows-core/core --output-on-failure
```

Windows portability notes baked into the build:
- `core/CMakeLists.txt` defines `NOMINMAX` and `_USE_MATH_DEFINES` project-wide for MSVC
  (OCCT pulls in `<windows.h>`, whose macros otherwise clobber `std::min`/`std::max`; MSVC
  hides `M_PI` behind `_USE_MATH_DEFINES`). `chudware_cam` and `chudware_firmware` also export
  `_USE_MATH_DEFINES` to their consumers.
- The core and host targets link `nlohmann_json` explicitly (no ambient system include on Windows).
- `test_cam` uses `temp_directory_path()` instead of a hardcoded `/tmp`.

## 3b. Build the core WITH the OCCT geometry kernel

```bat
cmake --preset windows-occt
cmake --build --preset windows-occt
ctest --test-dir build/windows-occt/core --output-on-failure
```

The first configure downloads OCCT 7.9.3 (`core/cmake/ProvisionOCCT.cmake`) and the pinned
BSD-3-Clause Polyhedral-FEA source under `third_party/`.

That build also produces the Polyhedral-FEA driver `polymesh.exe` in `build\windows-occt\bin\`,
beside `libchudware.dll` and `chudware-hostd.exe` (see `docs/reference/HOSTD.md`). To build
against a local Polyhedral-FEA checkout instead of the pin, add
`-DPOLYMESH_SOURCE_DIR=<checkout>` to the configure line (the `build.sh`
`CHUDWARE_POLYMESH_ROOT` env var is the POSIX front end for the same variable); the
configure then prints `Polyhedral-FEA: LOCAL SOURCE <dir> @ <sha>` instead of
`Polyhedral-FEA: PINNED FETCH …`.

## 4. GUI app (`host/`)

```bat
cmake -S host -B host\build-win -G Ninja -DCMAKE_BUILD_TYPE=Release -DCHUDWARE_LTO=ON -DCHUDWARE_ENABLE_CEF=OFF
cmake --build host\build-win
ctest --test-dir host\build-win --output-on-failure
```

`scripts/build_windows.ps1` runs the `windows-occt` core build and this Release/LTO host
build in one step, followed by developer staging and MCP registration when the local
interpreter is present. Every native builder/setup exit code is checked before the next
stage; the script restores its caller's location and compiler environment on success or
failure. Visual Studio environment capture preserves Unicode without a shared temporary
batch file. CI uses the same build trees with `host\build-windows`.

MSVC/Ninja LTO builds use one concurrent link job by default, while compilation
remains parallel. Concurrent engine-sized LTCG links exhausted the 8 GiB native
verification guest (`C1002`/`LNK1257`); the shared link pool bounds that concurrency
without disabling release optimization. An explicitly supplied
`CMAKE_JOB_POOL_LINK` remains the caller's policy, and existing pool definitions
are preserved. The generated miniz export and embedded firmware-source headers
are content-stable across unchanged configuration, so reconfiguring does not
invalidate their consumers or force an otherwise unnecessary engine relink.

Developer staging (`scripts/stage_windows.ps1`) requires the complete engine/host
binaries and refuses missing prerequisites before changing `dist`. The runtime wheel lock
and offline wheelhouse are optional: it stages them only when both are present, and fails if
only one is (see §5). Without them the dist runs from the `CHUDWARE_PYTHON` venv.
`robocopy` statuses 0–7 are successful; failure statuses are fatal. Staging success is not
qualification of an installed release.

- **`/FI fix_math.h` quoting (fatal C1083).** Under the **Ninja + MSVC** generator a quoted
  force-include path leaks literal quote chars into the filename. `host/CMakeLists.txt` passes
  `/FI${path}` unquoted so Ninja quotes the whole `/FI<path>` token, which `cl` unwraps.
- **Engine resolution (developer builds).** With `CHUDWARE_DEVELOPER_BUILD`, `cpp_core`
  (`services/worker/cpp_core.cpp`) tries `CHUDWARE_LIBCHUDWARE`, then `libchudware.dll` beside
  the exe, then the repo-relative `build\windows-occt\bin\libchudware.dll` (and a few legacy
  layouts), and sets the DLL directory to the chosen file's folder so OCCT's DLLs resolve.
  Non-developer (release) builds load only the signed-manifest-verified `libchudware.dll`
  beside the exe.
- **Worker fallback.** `AppState::tryStartWorker()` tries the managed worker first on every
  desktop platform; if it cannot start, it falls back to the in-process
  `cpp_core().ensureLoaded()` engine, whether or not a managed runtime is installed. CI's
  Windows GUI smoke forces that path (`CHUDWARE_WORKER_BIN` pointed at a missing file,
  `CHUDWARE_ENGINE_DAEMON=0`, `CHUDWARE_LIBCHUDWARE` = the job's
  `build\windows-occt\bin\libchudware.dll`) and requires the `CHUDWARE_SHOT=m1` scenario to
  succeed; without any library, `m1` must exit 1 and `chat` must still capture.
- **Graphics qualification.** Headless captures still require a working OpenGL 3.3 core
  WGL driver. A VM without WGL support exits before rendering; a successful C++ build
  does not prove the desktop surface. An isolated per-application software driver can
  exercise that surface, but does not qualify hardware GPU performance or become part
  of the shipped runtime. The M1 capture must adopt the saved mesh on reopen rather
  than report success from an intact feature tree while photographing an empty viewport.
- **glad** lives at `extern/glad` (vendored, compiled directly into the target).

## 5. Python runtime + tests

Development uses a plain virtual environment installed from `requirements.txt`:

```powershell
py -3.13 -m venv .venv-mcp
.venv-mcp\Scripts\python -m pip install -r requirements.txt
$env:CHUDWARE_PYTHON = "$PWD\.venv-mcp\Scripts\python.exe"
.venv-mcp\Scripts\python -X utf8 -m pytest services/agent -q --timeout=90
```

`CHUDWARE_PYTHON` tells the build and staging scripts which interpreter hosts the
Python services. On POSIX the equivalent is `python3 -m venv .venv-mcp &&
.venv-mcp/bin/python -m pip install -r requirements.txt`. Optional browser binaries and
simulation tools keep their explicit provisioning commands; dependency setup never
silently downloads or builds them.

### Optional: offline wheelhouse mode

Maintainers who need a hash-pinned, index-free runtime can prepare one with
`scripts/prepare_runtime_wheels.py`: its `candidate` step writes an unapproved review
document, and after the operator reviews and approves it, `prepare` emits a bundle with
`RUNTIME_WHEELS.lock` and a matching `wheelhouse/`. With that bundle,
`services/installer/provision.py --wheelhouse <dir>` (POSIX front end:
`CHUDWARE_WHEELHOUSE=<dir> scripts/provision_venv.sh`) installs only verified local wheels with
`--no-index --no-deps --require-hashes --only-binary=:all:`, and
`scripts/stage_windows.ps1` copies the lock and wheelhouse into `dist`. This is optional
hardening; the default developer flow above does not need it.

The wheelhouse regression smoke uses synthetic wheel ZIPs, temporary venvs and a
loopback-only fake index:
`python -m pytest services/installer/test_wheelhouse.py scripts/test_mcp_runtime_audit.py -q`.

Installed releases use compiled worker/MCP sidecars and do not provision Python; the build
interpreter must import `packaging/requirements-release.txt` for those sidecars (see
`PACKAGING_WINDOWS.md`). Native engine regression coverage is CTest in
`build/<preset>/core`.

## Troubleshooting / gotchas

- **`cl` / `cmake` "is not recognized"** — MSVC isn't on `PATH` in a normal shell. Build from the
  **x64 Native Tools Command Prompt for VS 2022** (Start menu) or run `vcvars64.bat` first.
- **`freecad_parity`** — historically this ctest case differed between MSVC and GCC on
  the point-on-ellipse solver case. Check the latest Windows CI row before treating a failure
  there as a new regression.
