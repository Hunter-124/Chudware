# Building Chudware on Windows

The `windows-core` (OCCT off) and `windows-occt` (OCCT + Polyhedral-FEA) presets configure
MSVC + Ninja builds in `CMakePresets.json`. The self-hosted Windows CI row
(`.github/workflows/native-build.yml`, `v*` tags and manual dispatch) is configured to build
`windows-occt` plus the `host/` GUI and run both CTest trees. That wiring is not itself
a claim that the latest run passed.
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
| `host\build-windows` | host GUI tree used by `native-build.yml` and `release.yml` |
| `host\build-win` | host GUI tree used by `scripts/build_windows.ps1`, `nightly-release.yml` and `services/installer/build_release.py` (packaging reads `host\build-win\bin\chudware.exe`) |

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

Developer staging requires the complete engine/host binaries and reviewed runtime inputs,
including `vendor/python/RUNTIME_WHEELS.lock`; it refuses missing prerequisites before
changing `dist`. `robocopy` statuses 0–7 are successful; failure statuses are fatal.
Staging success is not publisher approval or qualification of the installed release.

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

Development provisioning uses **Python 3.11** and a complete reviewed offline
wheelhouse, not `pip install -r requirements.txt`. Direct pip use of the root
declarations is index-disabled as a defense against private-package fallback.

```powershell
$base = (& py -3.11 -c "import sys;print(sys.executable)")
$entry = "import sys;sys.path.insert(0,sys.argv[1]);from services.installer.provision import main;sys.exit(main(sys.argv[2:]))"
& $base -I -S -c $entry (Get-Location).Path --base-python $base --venv-dir .venv `
    --wheelhouse C:\reviewed-inputs\wheelhouse
& $base -I -S -c $entry (Get-Location).Path --check-ready `
    --venv-dir .venv --wheelhouse C:\reviewed-inputs\wheelhouse
# Test-only tools must be additional reviewed target wheels, not post-install mutations.
.\.venv\Scripts\python -X utf8 -m pytest services/agent -q --timeout=90
```

On POSIX the same flow is `CHUDWARE_WHEELHOUSE=/reviewed/wheelhouse
VENV_DIR=.venv-mcp PYTHON=python3.11 scripts/provision_venv.sh`. No private
checkout is installed editable, no wheel is built during provisioning, and pip
receives only verified local file URIs with `--no-index --no-deps
--require-hashes --only-binary=:all:`. Unlisted wheelhouse candidates are ignored.
Optional browser binaries and simulation tools retain their explicit provisioning
commands; dependency setup never silently downloads/builds them.

### Reviewed runtime identity prerequisite

The trusted source/development payload must contain
`vendor/python/RUNTIME_WHEELS.lock`, independently authenticated and reviewed,
not supplied by the wheelhouse owner. Its JSON contract is:

- `schema`: `chudware-runtime-wheels-v1`.
- `requirements_sha256`: full SHA-256 of the root declarations, including comments
  and offline options; changing them invalidates the lock.
- `chudcode_source`: `repository` and forty-hex `repository_commit`, exactly matching
  `vendor/chudcode/CHUDCODE_ARTIFACT.lock`.
- `targets`: objects keyed by `<sys_platform>-<normalized machine>-cp<major><minor>`
  (for example `win32-x86_64-cp311`, `linux-x86_64-cp311`, `darwin-arm64-cp311`).
  Each target has `wheels`, a nonempty list of objects with exactly `filename`,
  `name`, `version`, and full `sha256`. Every entry names one compatible wheel;
  duplicate distribution names, source distributions, links, direct dependency
  URLs and mismatched metadata are refused.

Every declared dependency and applicable transitive dependency (including selected
extras) must have a satisfying reviewed wheel. All listed wheels' dependency
closures and selected interpreter tags/Requires-Python are checked before venv
creation or pip installation. Wheels are copied to a disposable verified snapshot;
pip never searches the supplied wheelhouse or an index. Ready-runtime reuse requires
the full lock digest, target, installed versions, PEP 610 wheel hashes and actual
installed package/library bytes compared to approved archive members, not
self-reported RECORD hashes. Old editable/public-index SDKs are not accepted.
Unapproved `.pth`, `sitecustomize` and `usercustomize` startup hooks are rejected by
an isolated `-I -S` metadata/bytes probe before package imports or compilation.
Unlisted package-tree files are rejected. Generated `.pyc` files must match the
trusted interpreter's builtin compilation of the authenticated source; neither
source nor a build backend is executed for that comparison.
Pip-generated RECORD/INSTALLER/REQUESTED metadata and rewritten script/header
installations are excluded from the package-byte comparison; their wheel inputs
remain authenticated. Baseline bootstrap packages/hooks must exactly match the
selected trusted base interpreter's ensurepip wheel payloads unless replaced by a
reviewed runtime wheel. **Trust prerequisite:** the system/portable Python binary,
the candidate venv interpreter (an original link/copy of that base), stdlib and
ensurepip payload must already be authenticated by the operator; this flow neither
fetches Python nor manufactures that trust from a version string.
An unrecognized existing venv is left unchanged; `--force` explicitly permits its
replacement only after all reviewed wheel inputs pass validation.

Linux/macOS/Windows sidecar compiler scripts use a site-disabled `--check-inputs`
gate before Nuitka, requiring the same lock/target, stamp and authenticated installed
package bytes, and explicitly include `chudcode_rpc`. The approved target must also
satisfy every applicable declaration/dependency in `packaging/requirements-release.txt`
before any compiler/tool package is imported. Provision release and test tools as
additional reviewed target wheels through the same locked source flow, not a
subsequent public pip installation.
Customer payloads remain source-free and do not receive a wheelhouse/provisioner.

**Current missing inputs:** there is no committed complete `RUNTIME_WHEELS.lock`
or authenticated `chudcode_rpc-0.3.1` wheel identity. The native Chudcode executable
lock's source commit does not authenticate SDK wheel bytes. Provisioning and
development payload staging therefore fail closed until maintainers authenticate
the private wheel and complete per-target dependency sets and commit that reviewed
lock. Do not generate an identity by executing an untrusted backend or deriving a
hash from a public candidate. The inspected local SDK `uv.lock` is editable and
still describes version 0.3.0; it is not a complete managed-runtime lock.

The isolated regression smoke uses inert synthetic wheel ZIPs, temporary venvs and
a loopback-only fake public index. Run
`python -m pytest services/installer/test_wheelhouse.py scripts/test_mcp_runtime_audit.py -q`
using the integrator's existing test interpreter in a network-disabled disposable
environment. It installs no real SDK, invokes no build backend and must observe
zero fake-index requests for positive installs and absent/tampered candidates.

Customer installs use compiled worker/MCP sidecars and do not provision Python; the build
interpreter must import `packaging/requirements-release.txt` for those sidecars (see
`PACKAGING_WINDOWS.md`). The retired `engine_oracle` suite is not a Windows test entry point;
native engine regression coverage is CTest in `build/<preset>/core`.

## Troubleshooting / gotchas

- **`cl` / `cmake` "is not recognized"** — MSVC isn't on `PATH` in a normal shell. Build from the
  **x64 Native Tools Command Prompt for VS 2022** (Start menu) or run `vcvars64.bat` first.
- **`freecad_parity`** — historically (T-WIN1) this ctest case differed between MSVC and GCC on
  the point-on-ellipse solver case. Check the latest Windows CI row before treating a failure
  there as a new regression.
- **Legacy `UnicodeEncodeError` on `work.py board`** was a cp1252-console bug, fixed on `master`
  (`work.py` forces UTF-8 stdout). If you hit it on an old checkout, pull latest.
