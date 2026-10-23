# `core/` — the pure-C++ engine (libchudware)

The pure-C++ engine (`libchudware`) that replaced the former Python engine.
The host GUI does not link it. By default the GUI talks over IPC to the
`chudware-hostd` daemon (built from `core/hostd/`), which owns the loaded
libchudware and serves every client. The GUI only `dlopen`s libchudware in
process (`services/worker/engine_backend.h`, InProcCore) as a fallback, when
`CHUDWARE_ENGINE_DAEMON=0` is set or the daemon stays unreachable after bounded
retries.

## Layout

```
core/
  Geometry & constraint kernel
    geom/     OCCT B-rep kernel: feature compilers, tessellation, AICM emitter, STEP IO
    sketch/   sketch.{h,cpp} + sketch3d: entity parameterization + constraints (2D and 3D)
    solver/   nsolve.{h,cpp}   damped-LM constraint solver core
    mate/     assembly mates + mate solver (+ corpus fixtures)
  Document & API
    doc/      document model, sessions, formats/import/export, validation, picks, drawings
    api/      libchudware.{h,cpp}: the C ABI surface every client binds to
  Simulation
    sim/      FEA/CFD runs, Polyhedral-FEA bridge, SPICE, fatigue/adaptive studies
    twin/     digital-twin runtime (wide-math state propagation)
  Manufacturing & domain engines
    cam/      toolpaths, operations, posts, stock simulation
    print/    3D-print model/slicing prep
    ecad/     PCB container, impedance, zone fill, autoroute, MCAD sync
    piping/   piping schema, flow solve, CFD handoff
    hydraulic/ hydraulic schema + solve + BOM
    structural/ structural solver, checks, estimates, RPC
    mechanical/ mechanical-design helpers
    materials/ material database
    tolerance/ GD&T stackup analysis
    dfm/      DFM process profiles
    parts/    standard-parts catalog, generation, placement
    sourcing/ vendor registry, spec match, quotes, costing, BOM
    firmware/ MCU emulator, sensor models, toolchains, co-verify
  Agent, daemon & device
    agent/    copilot runtime: roles, local providers, auth, orchestration
    hostd/    chudware-hostd daemon: server, doc registry, worker bridge, mfg lane
    device/   machine transports: GRBL protocol/adapter, emulator, capability
  Infrastructure
    util/     paths, process isolation, resource governor, AICM handoff, Ed25519 (TweetNaCl)
    security/ signed payload-manifest integrity, release signing keys
    manufacturing/ authorization arbiter, gates, leases, journal
    prof/     profiling: phase timers, tree, sampler
    cmake/    Provision*.cmake auto-provisioners (OCCT, Polyhedral-FEA, OpenNURBS, lib3mf, …)
    data/     bundled data (materials)
    third_party/ vendored sources (rhino3dm-src)
    docs/     kicad_reference.md
  Tests
    tests/    doctest suites: test_nsolve.cpp, test_sketch.cpp, plus ~100 subsystem tests
  CMakeLists.txt            build: Eigen + doctest + nlohmann/json via FetchContent;
                            -DCHUDWARE_WITH_OCCT=ON adds the OCCT kernel (cmake/ProvisionOCCT.cmake:
                            system OCCT on Linux/macOS, downloaded 7.9.3 on Windows)
```

## Build & test

Linux/macOS: `./build.sh core` rebuilds the trees that `scripts/gate.sh`, the GUI and
`CHUDWARE_LIBCHUDWARE` consumers load, `core/build-occt-<platform>` and
`core/build-<platform>`, plus `chudware-hostd` from the same tree. If neither tree is
configured, it configures the host's preset (`linux-core`, `macos-arm64`,
`macos-x86_64` in the root `CMakePresets.json`), whose `build/<preset>/` output
nothing loads. To configure a consumed tree by hand (as the gate does):

```
cmake -S core -B core/build-linux -G Ninja -DCMAKE_BUILD_TYPE=Release -DCHUDWARE_WITH_OCCT=ON
ninja -C core/build-linux
ctest --test-dir core/build-linux --output-on-failure
```

`CMAKE_BUILD_TYPE=Release` matters: the single-config Ninja generator otherwise builds
-O0. Windows uses the `windows-core` (MSVC, OCCT off) or `windows-occt` preset from an
x64 Native Tools prompt; see `docs/reference/BUILD_WINDOWS.md`.

CMake 4.x note: doctest's `cmake_minimum_required(3.0)` is bypassed via
`SOURCE_SUBDIR` (header-only include), so **no** `CMAKE_POLICY_VERSION_MINIMUM`
override is needed — and must not be set (it poisons the cache; wipe the build tree
if it ever gets in).

## Solver status

The solver and sketch subsystem is implemented in C++ and covered by the
`test_nsolve` and `test_sketch` suites:
- nsolve numeric core: residual/Jacobian registry, damped Levenberg-Marquardt,
  null-space-projected constrained drag, QR-rank DOF / redundant / conflicting
  diagnostics (chronological attribution).
- sketch entities: point, line, circle, arc, ellipse, ellipse_arc, parabola,
  conic, slot, arc_slot, spline, style_spline, text (allocation + handle
  resolution + extraction; conic-family derived-point handles carry their
  analytic / numeric chain-rule Jacobians).
- sketch constraints: coincident, horizontal, vertical, parallel, perpendicular,
  collinear, coradial, concentric, equal (line/slot length + circle/arc radius),
  midpoint, symmetric, angle, point_on (line/circle/arc), fix (whole-entity +
  sub-handle + conic-family derived-handle pin), tangent (line-circle AND
  circle-circle, internal/external branch frozen as-drawn), distance — point-point
  (aligned + axis h/v directional), point-line, AND the C50 `cond` (min/max rim
  with the radius as a solver variable on either ref), radius, diameter (incl.
  slot/arc_slot width mapping w=2r / w=d); SVD-nullspace status coloring. Polar
  (r, θ) placement is expressed by `angle` + `distance`-to-origin (proven by
  `corpora/sketch/ported_polar_point.json` landing a point at (r·cosθ, r·sinθ)).

Additional sketch coverage continues to land directly in the C++ solver and its
unit and corpus fixtures.

## Methodology

Every analytic Jacobian is covered by the C++ tests and corpus fixtures. When
adding a new constraint:
1. implement the residual and analytic Jacobian in `core/sketch/`,
2. add a fixture to `corpora/sketch/`,
3. add or update the corresponding C++ solver test.
