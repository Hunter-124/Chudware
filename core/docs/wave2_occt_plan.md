# Wave 2 OCCT Integration Plan — Prebuilt Binary on Windows / MSVC x64

> Status: **PLAN** (not yet installed). Review this doc, then execute the checklist.
> Wave 2 goal (from `~/.claude/plans/im-planning-on-doing-noble-glacier.md`): link OCCT into the
> process, port feature compilers → `core/geom/`, port the tessellation/AICM emitter, and replace
> the Python modeling worker with in-process `libchudware` calls.

---

## 1. Recommended Package

### Choose: OCCT 7.9.3 — NOT 8.0.0

**Version:** `7.9.3` (released 2025-12-05, latest stable 7.x)

**Why 7.9.3, not 8.0.0 (released 2026-05-07):**
OCCT 8.0 has significant breaking changes that would require immediate port work:

- `BRepMesh_IncrementalMesh` constructor signature changed (the `adaptiveMin` parameter removed,
  `BRepMesh_FastDiscret::Parameters` moved to `IMeshTools_Parameters`). Our tessellate port copies
  `engine/doc/tessellate.py` directly — that code was written against OCP 7.8.1 and uses the 7.x
  call signature `BRepMesh_IncrementalMesh(shape, lin_defl, False, ang_defl, True).Perform()`.
- `BRepAlgoAPI_BooleanOperation`: `BuilderCanWork`, `FuseEdges`, `RefineEdges` removed.
- `BRepAlgo_BooleanOperations` and `BRepAlgo_DSAccess` removed entirely.
- 8.0 provides automated migration scripts (`adm/scripts/migration_800/`) but they run on source —
  not a day-zero priority.

**Recommendation: start on 7.9.3, plan a 8.x bump as a future non-breaking upgrade once the port
is stable.**

### Exact packages to fetch

**Primary OCCT binary** (headers + release `.lib` + `.dll`):
```
opencascade-7.9.3-vc14-64.zip
```
Direct URL: `https://github.com/Open-Cascade-SAS/OCCT/releases/tag/V7_9_3`

If you want release + debug in one archive (larger, ~250 MB):
```
opencascade-7.9.3-vc14-64-with-debug.zip
```

**3rd-party dependencies** (FreeType, FreeImage, TBB, Tcl/Tk bundled — download separately):
```
3rdparty-vc14-64.zip
```
Also from the same GitHub release tag, or from:
`https://dev.opencascade.org/resources/download/3rd-party-components`

The `combined` variant (`opencascade-7.9.3-vc14-64-combined.zip`) bundles the 3rdparty inside the
same zip — that is the most convenient option if available at the release tag. Prefer it.

**VC tag naming note:** despite the "vc14" label (originally VS2015), Open CASCADE's CI builds these
binaries with VS2022 (MSVC v143 / `vc143`). The "vc14" in the filename is a historical artifact
meaning "VS2015-compatible ABI class" — the actual DLLs are produced by VS2022. They are fully
compatible with our VS2022 MSVC toolchain.

---

## 2. License Confirmation

OCCT 7.9.x is distributed under **GNU LGPL v2.1 with the Open CASCADE Exception 1.0**
(`SPDX: LGPL-2.1-only WITH OCCT-exception-1.0`). The exception text is at
`https://github.com/Open-Cascade-SAS/OCCT/blob/master/OCCT_LGPL_EXCEPTION.txt`.

**What this means for us:** dynamic linking (DLLs) is the safe path. The LGPL exception explicitly
permits incorporating OCCT header material into object code and linking the resulting object file
against OCCT shared libraries without triggering the copyleft obligations that would otherwise
require source disclosure of our own code. As long as we:

1. Ship OCCT as DLLs (not statically linked), keeping the library replaceable by the user.
2. Include a copy of the LGPL-2.1 license text and the OCCT exception text alongside any
   distribution (a `licenses/` folder next to the exe is sufficient).
3. Give "prominent notice" in our docs/about screen that the product uses OCCT (e.g., "Powered by
   Open CASCADE Technology — opencascade.com").

…we have no copyleft obligation on our own source. Internal/private builds (no distribution) have
no attribution obligations at all.

---

## 3. CMake Integration

### 3.1 Directory layout after extraction

Extract both zips to a single root, e.g.:

```
C:\OCCT\
  opencascade-7.9.3\          ← from opencascade-7.9.3-vc14-64.zip
    cmake\                    ← OpenCASCADEConfig.cmake lives HERE
    inc\                      ← #include <BRep_Tool.hxx> etc.
    win64\
      vc14\
        bin\                  ← TKernel.dll, TKBRep.dll, ... (release)
        lib\                  ← TKernel.lib, TKBRep.lib, ...
        bind\                 ← TKerneld.dll, ... (debug — only in *-with-debug* package)
        libd\                 ← TKerneld.lib, ... (debug)
    data\
    resources\
    doc\
  3rdparty\                   ← from 3rdparty-vc14-64.zip (or from combined zip)
    freetype-2.5.5\
      bin\  lib\  include\
    freeimage-3.17.0\
      bin\  lib\  include\
    tbb-4.x\
      bin\  lib\  include\
    tcl-8.6\
      bin\  lib\  include\
    tk-8.6\
      ...
```

The cmake config file path is:
```
C:\OCCT\opencascade-7.9.3\cmake\OpenCASCADEConfig.cmake
```

So `OpenCASCADE_DIR` = `C:\OCCT\opencascade-7.9.3\cmake`.

### 3.2 Cache variable to expose the root

Add to `core/CMakeLists.txt` (and later `host/CMakeLists.txt`) at the top, before any
`find_package`:

```cmake
# --- OCCT (Wave 2) -----------------------------------------------------------
# Set OCCT_ROOT on the cmake command line:
#   cmake -S core -B core/build -G "Visual Studio 17 2022" -A x64
#          -DOCCT_ROOT=C:/OCCT/opencascade-7.9.3
# or export it as an env var / persist it in a CMakePresets file.
set(OCCT_ROOT "" CACHE PATH "Root of the prebuilt OpenCASCADE installation")

if(NOT OCCT_ROOT STREQUAL "")
  set(OpenCASCADE_DIR "${OCCT_ROOT}/cmake" CACHE PATH "" FORCE)
endif()

find_package(OpenCASCADE REQUIRED)
```

### 3.3 Toolkits to link and why

After `find_package(OpenCASCADE REQUIRED)` succeeds, individual toolkit targets are available with
their plain `TK*` names (the prebuilt config exports them without a namespace prefix; the
`occt::TK*` form used by Hunter/vcpkg comes from those package managers wrapping the targets).

```cmake
# --- libchudware_geom: OCCT-backed geometry + tessellation ---------------------
add_library(chudware_geom_occt STATIC
  geom/occt_box.cpp          # smoke test first
  geom/tessellate.cpp        # port of engine/doc/tessellate.py
  # ... feature compilers added incrementally
)

target_include_directories(chudware_geom_occt PUBLIC
  ${OpenCASCADE_INCLUDE_DIR}  # set by find_package → typically OCCT_ROOT/inc
  ${CMAKE_CURRENT_SOURCE_DIR})

target_link_libraries(chudware_geom_occt PUBLIC
  # --- kernel / math ---
  TKernel           # Standard (handles, memory) + math foundation
  TKMath            # gp_*, Bnd_*, math primitives

  # --- topology + geometry ---
  TKBRep            # TopoDS_*, BRep_Builder, BRep_Tool
  TKG2d             # Geom2d curves (for pcurves on faces)
  TKG3d             # Geom3d surfaces and curves
  TKGeomBase        # Basic geometric algorithms
  TKGeomAlgo        # Higher geometric algorithms (GCPnts, GeomAPI)
  TKTopAlgo         # TopExp, TopTools, BRepAlgoAPI helpers
  TKShHealing       # ShapeAnalysis, ShapeUpgrade (used by tessellate)

  # --- primitives + booleans + fillets + offsets ---
  TKPrim            # BRepPrimAPI_MakeBox/Cylinder/Cone/Sphere/Torus/Wedge
  TKBO              # BOPAlgo (underlying boolean engine)
  TKBool            # BRepAlgoAPI_Fuse/Cut/Common/Section
  TKFillet          # BRepFilletAPI_MakeFillet/Chamfer
  TKOffset          # BRepOffsetAPI_MakeThickSolid/MakeShell/MakePipe/Loft

  # --- tessellation / meshing ---
  TKMesh            # BRepMesh_IncrementalMesh + BRepLib_ToolTriangulatedShape

  # --- STEP / IGES / STL exchange ---
  TKXSBase          # Base XCAF transfer framework
  TKSTEP            # STEPControl_Writer/Reader (the umbrella)
  TKSTEP209         # AP209 (structural FEM, optional but part of TKSTEP group)
  TKSTEPAttr        # STEP attribute dictionaries
  TKSTEPBase        # Core STEP schema
  TKIGES            # IGESControl_Writer/Reader
  TKSTL             # BRepMesh_IncrementalMesh → STL writer (StlAPI_Writer)
)
```

**Python OCP → C++ OCCT mapping:**

| Python (engine/doc/tessellate.py or evaluate.py)               | C++ toolkit |
|----------------------------------------------------------------|-------------|
| `BRepMesh_IncrementalMesh`, `BRepLib_ToolTriangulatedShape`    | TKMesh      |
| `TopExp.MapShapes_s`, `TopTools_IndexedMapOfShape`             | TKTopAlgo   |
| `BRep_Tool.*`, `TopoDS.*`, `TopLoc_Location`                  | TKBRep      |
| `BRepAdaptor_Surface`, `BRepAdaptor_Curve`                     | TKBRep      |
| `GCPnts_TangentialDeflection`                                  | TKGeomAlgo  |
| `GeomAbs_*` constants                                          | TKGeomBase  |
| `BRepGProp.VolumeProperties_s`, `GProp_GProps`                 | TKBRep (BRepGProp header is in TKBRep) |
| `BRepAlgoAPI_Section`, `BRepExtrema_DistShapeShape`            | TKBool      |
| `BRepCheck_Analyzer`                                           | TKBRep      |
| `ShapeAnalysis_Surface`, `BRepLProp_SLProps`                   | TKShHealing / TKBRep |
| `gp_Dir`, `gp_Pln`, `gp_Vec`, `gp_Pnt`                       | TKMath      |
| `BRepPrimAPI_MakeBox/Cylinder/…`                               | TKPrim      |
| `BRepAlgoAPI_Fuse/Cut/Common`                                  | TKBool      |
| `BRepFilletAPI_MakeFillet`                                     | TKFillet    |
| `BRepOffsetAPI_MakePipe/ThickSolid`                            | TKOffset    |
| `STEPControl_Writer/Reader`, `IGESControl_*`                   | TKSTEP, TKIGES |
| `StlAPI_Writer`                                                | TKSTL       |

> Note on `BRepGProp`: the Python code imports `from OCP.BRepGProp import BRepGProp`. In OCCT C++
> the header is `#include <BRepGProp.hxx>` and the class lives in the `TKBRep` toolkit. No separate
> library is required.

### 3.4 Putting it all together in core/CMakeLists.txt

```cmake
# At the top of core/CMakeLists.txt, after the existing FetchContent blocks:

# --- OCCT (Wave 2) -----------------------------------------------------------
set(OCCT_ROOT "" CACHE PATH "Root of the prebuilt OpenCASCADE 7.9.3 installation")
if(NOT OCCT_ROOT STREQUAL "")
  set(OpenCASCADE_DIR "${OCCT_ROOT}/cmake" CACHE PATH "" FORCE)
endif()
find_package(OpenCASCADE REQUIRED)
message(STATUS "OpenCASCADE ${OpenCASCADE_VERSION} found; include=${OpenCASCADE_INCLUDE_DIR}")

# --- libchudware_geom_occt (smoke test + tessellate) ----------------------------
add_library(chudware_geom_occt STATIC
  geom/occt_smoke.cpp)

target_include_directories(chudware_geom_occt PUBLIC
  ${OpenCASCADE_INCLUDE_DIR}
  ${CMAKE_CURRENT_SOURCE_DIR})

target_link_libraries(chudware_geom_occt PUBLIC
  TKernel TKMath
  TKBRep TKG2d TKG3d TKGeomBase TKGeomAlgo TKTopAlgo TKShHealing
  TKPrim TKBO TKBool TKFillet TKOffset
  TKMesh
  TKXSBase TKSTEP TKSTEP209 TKSTEPAttr TKSTEPBase TKIGES TKSTL)

# --- smoke test executable ---------------------------------------------------
add_executable(test_occt_smoke geom/test_occt_smoke.cpp)
target_include_directories(test_occt_smoke PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_occt_smoke PRIVATE chudware_geom_occt)
set_target_properties(test_occt_smoke PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME occt_smoke COMMAND test_occt_smoke)
```

---

## 4. Runtime DLL Deployment

### 4.1 Which DLLs are required at runtime

All DLLs from `%OCCT_ROOT%\win64\vc14\bin\` that correspond to the linked toolkits, plus the
3rdparty DLLs from `%3RDPARTY_ROOT%\*\bin\`. The minimal set for our use is:

**OCCT TK DLLs (release):**
```
TKernel.dll    TKMath.dll
TKBRep.dll     TKG2d.dll     TKG3d.dll     TKGeomBase.dll
TKGeomAlgo.dll TKTopAlgo.dll TKShHealing.dll
TKPrim.dll     TKBO.dll      TKBool.dll    TKFillet.dll  TKOffset.dll
TKMesh.dll
TKXSBase.dll   TKSTEP.dll    TKSTEP209.dll TKSTEPAttr.dll TKSTEPBase.dll
TKIGES.dll     TKSTL.dll
```

**3rdparty DLLs** (from `3rdparty\*\bin\`):
```
freetype.dll      # required — font rendering in OCCT visualization (even if we don't use V3d)
TBB.dll           # optional but bundled; OCCT may use it for parallel meshing
```
FreeImage, Tcl/Tk, FFmpeg are only needed if you use OCCT's visualization (V3d, AIS) or DRAW
harness. We do not use those — FreeImage and Tcl/Tk can be omitted for the geom-only path.

**Debug DLLs** (only if building Debug config): same names with `d` suffix — `TKerneld.dll`, etc.,
from `win64\vc14\bind\`.

### 4.2 CMake post-build copy (mirror the host's asset-copy pattern)

In `core/CMakeLists.txt` (or `host/CMakeLists.txt` when the host links it):

```cmake
# --- Copy OCCT + 3rdparty DLLs next to the exe (Wave 2) ---------------------
# Mirror the existing "Copying assets" pattern in host/CMakeLists.txt.
# OCCT_ROOT and OCCT_3RDPARTY_ROOT are cache vars set at configure time.

set(OCCT_3RDPARTY_ROOT "" CACHE PATH "Root of the OCCT 3rdparty tree (3rdparty-vc14-64)")

if(NOT OCCT_ROOT STREQUAL "")
  set(_occt_bin_rel "${OCCT_ROOT}/win64/vc14/bin")
  set(_occt_bin_dbg "${OCCT_ROOT}/win64/vc14/bind")

  add_custom_command(TARGET test_occt_smoke POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_directory
            "$<IF:$<CONFIG:Debug>,${_occt_bin_dbg},${_occt_bin_rel}>"
            "$<TARGET_FILE_DIR:test_occt_smoke>"
    COMMENT "Copying OCCT DLLs next to $<TARGET_FILE:test_occt_smoke>")
endif()

if(NOT OCCT_3RDPARTY_ROOT STREQUAL "")
  # Copy freetype and TBB bins (the only mandatory 3rdparty DLLs for geom-only use)
  foreach(_tp freetype-2.5.5 tbb-4.x)
    set(_tp_bin "${OCCT_3RDPARTY_ROOT}/${_tp}/bin")
    if(EXISTS "${_tp_bin}")
      add_custom_command(TARGET test_occt_smoke POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_directory
                "${_tp_bin}"
                "$<TARGET_FILE_DIR:test_occt_smoke>"
        COMMENT "Copying ${_tp} DLLs")
    endif()
  endforeach()
endif()
```

When the `Chudware` host target eventually links `libchudware_geom_occt`, attach the same
`add_custom_command` to `Chudware` as a `POST_BUILD` step (identical pattern to the existing
`"Copying assets"` command in `host/CMakeLists.txt` lines 168–172).

---

## 5. Step-by-Step Wave-2 Bring-Up Checklist

### Step 0 — Agree on install path

Pick one of:
- `C:\OCCT\opencascade-7.9.3\` — short, no spaces, survives reboots ✓
- `C:\Users\dev\Desktop\AI-opencad\third_party\occt\` — colocated with repo (do NOT commit 
  the binaries; add `third_party/occt/` to `.gitignore`)

We recommend `C:\OCCT\` to avoid PATH-length issues on Windows.

### Step 1 — Download

From `https://github.com/Open-Cascade-SAS/OCCT/releases/tag/V7_9_3` download:

```
opencascade-7.9.3-vc14-64-combined.zip   ← OCCT + 3rdparty bundled (preferred)
```
OR separately:
```
opencascade-7.9.3-vc14-64.zip
3rdparty-vc14-64.zip
```

Alternate mirror: `https://dev.opencascade.org/release/previous` (select 7.9.3, Windows 64-bit).

### Step 2 — Extract

```powershell
# Expand to C:\OCCT\
Expand-Archive -Path "opencascade-7.9.3-vc14-64-combined.zip" -DestinationPath "C:\OCCT"
# Result: C:\OCCT\opencascade-7.9.3\  and  C:\OCCT\3rdparty\  (or similar)
```

Verify the cmake config is present:
```powershell
Test-Path "C:\OCCT\opencascade-7.9.3\cmake\OpenCASCADEConfig.cmake"  # must be True
```

### Step 3 — Configure CMake

```powershell
# From the repo root:
cmake -S core -B core/build `
      -G "Visual Studio 17 2022" -A x64 `
      -DOCCT_ROOT="C:/OCCT/opencascade-7.9.3" `
      -DOCCT_3RDPARTY_ROOT="C:/OCCT/3rdparty"
```

CMake should print:
```
-- OpenCASCADE 7.9.3 found; include=C:/OCCT/opencascade-7.9.3/inc
```

If `find_package` fails: verify `OpenCASCADE_DIR` is being set and that
`C:\OCCT\opencascade-7.9.3\cmake\OpenCASCADEConfig.cmake` exists. On a bad extraction the cmake
subdir is sometimes inside a nested folder — check with:
```powershell
Get-ChildItem C:\OCCT -Recurse -Filter "OpenCASCADEConfig.cmake"
```

### Step 4 — Write the smoke-test source files

Create `core/geom/occt_smoke.cpp` (the library stub — empty for now, or with one include):
```cpp
// core/geom/occt_smoke.cpp
#include <BRepPrimAPI_MakeBox.hxx>  // verifies headers resolve
```

Create `core/geom/test_occt_smoke.cpp`:
```cpp
// core/geom/test_occt_smoke.cpp
// Smoke test: make a box, mesh it, assert volume == side^3
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include "doctest/doctest.h"

#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>

TEST_CASE("OCCT smoke: box volume") {
    const double side = 10.0;  // mm
    BRepPrimAPI_MakeBox box(side, side, side);
    TopoDS_Shape shape = box.Shape();

    // Tessellate (mirrors tessellate.py call signature for OCCT 7.x)
    BRepMesh_IncrementalMesh mesh(shape,
        /*lin_deflection=*/0.05,
        /*is_relative=*/Standard_False,
        /*ang_deflection=*/0.35,
        /*is_in_parallel=*/Standard_True);
    mesh.Perform();
    CHECK(mesh.IsDone());

    // Volume (mirrors evaluate.py: BRepGProp.VolumeProperties_s)
    GProp_GProps props;
    BRepGProp::VolumeProperties(shape, props);
    const double vol = props.Mass();  // mm^3 for unit density
    CHECK(vol == doctest::Approx(side * side * side).epsilon(1e-6));
}
```

### Step 5 — Build and run the smoke test

```powershell
cmake --build core/build --config Release --target test_occt_smoke
ctest --test-dir core/build -C Release -R occt_smoke --output-on-failure
```

Expected output:
```
[doctest] test cases: 1 | 1 passed | 0 failed
```

If you get `The application was unable to start correctly (0xc000007b)` or a missing-DLL dialog,
the DLL copy step didn't fire — check the `OCCT_ROOT` and `OCCT_3RDPARTY_ROOT` cache vars and
re-run the build.

### Step 6 — Port tessellate.py → core/geom/tessellate.cpp

Priority mapping of `engine/doc/tessellate.py` to C++ (each call in Python → same call in C++
with same semantic):

```
tessellate_body()
  → BRepMesh_IncrementalMesh(shape, lin_defl, false, ang_defl, true).Perform()
  → TopExp::MapShapes(shape, TopAbs_FACE, face_map)
  → BRep_Tool::Triangulation(face, loc) + loc.Transformation()
  → BRepLib_ToolTriangulatedShape::ComputeNormals(face, tri)
  → tri->Node(i) .Transform(trsf)  /  tri->Normal(i)
  → tri->Triangle(i).Get(n1,n2,n3)
  → BRep_Tool::PolygonOnTriangulation(edge, tri, loc)  [edge polylines]
  → GCPnts_TangentialDeflection fallback
  → BRepAdaptor_Surface(face).GetType()  for surf_type codes
  → BRepAdaptor_Curve(edge).GetType()    for curve_type codes
  → BRep_Tool::Pnt(vertex)              for B-rep vertex xyz
```

The Python tessellate writes the AICM binary using `struct.pack`. The C++ port writes the same
format using `fwrite` / `std::vector<uint8_t>` with identical little-endian layout — use
`docs/contracts/mesh_format.md` as the contract and `docs/contracts/fixtures/box.aicm` as the
golden fixture (byte-identical gate).

### Step 7 — Wire into the AICM differential harness

Once tessellate.cpp passes the `box.aicm` golden fixture, extend the Wave-1 differential harness
(`tools/solver_diff.py` pattern) to run:
- Python: `engine/doc/tessellate.py` → AICM bytes
- C++: `sketch_cli`-style binary that reads a STEP file and writes AICM bytes

Diff outputs within mesh tolerance (not byte-identical for arbitrary shapes, but `box.aicm` should
be byte-identical since the same meshing parameters produce deterministic triangulation on a box).

---

## 6. Gotchas

### 6.1 MSVC runtime: /MD is mandatory

The prebuilt OCCT DLLs are compiled with `/MD` (MSVC multi-threaded DLL runtime). Our
`core/CMakeLists.txt` must not use `/MT` (static runtime). Currently we do not set `/MT` — the
default for MSVC in CMake is `/MD` for Release, `/MDd` for Debug. Do not add
`CMAKE_MSVC_RUNTIME_LIBRARY` static overrides or the linker will throw LNK2038 "mismatch detected
for RuntimeLibrary."

### 6.2 Debug vs. release library names

In the prebuilt tree, **debug libraries have a `d` suffix**: `TKerneld.lib` / `TKerneld.dll`.
CMake's `find_package(OpenCASCADE)` with the shipped config handles this automatically via
`IMPORTED_LOCATION_DEBUG` / `IMPORTED_LOCATION_RELEASE` in the generated targets — you do not
need to manually select the suffix. This only works if you use the imported target names (`TKernel`
etc.) and NOT raw `-l` flags. The `$<CONFIG:Debug>` generator expression in the DLL copy command
above handles selecting `bind\` vs `bin\` at build time.

### 6.3 The "vc14" ABI tag

OCCT uses "vc14" to mean "VS2015 C++ ABI baseline," but the CI packages are built with VS2022.
All VS2015/2017/2019/2022 MSVC builds share the same `_MSC_VER >= 1900` ABI for the STL types
that cross the OCCT boundary (OCCT avoids STL in its public API for exactly this reason — it uses
`Handle<>`, `Standard_*`, `TCollection_*` types). This means our VS2022 build links correctly
against these DLLs; do not let the "vc14" label cause concern.

### 6.4 CMake 4.x policy quirk

Our existing `core/CMakeLists.txt` already handles the doctest `cmake_minimum_required(3.0)` issue
via `SOURCE_SUBDIR`. The `OpenCASCADEConfig.cmake` shipped with 7.9.3 uses CMake 3.x-era patterns
(`include_directories`, old `find_library` signatures). Under CMake 4.x these emit deprecation
warnings but still work. If warnings become errors in a future CMake upgrade, suppress with:
```cmake
cmake_policy(SET CMP0115 NEW)  # add only if needed
```
Do NOT set `CMAKE_POLICY_VERSION_MINIMUM` — that variable poisons the cache (see `core/README.md`).

### 6.5 3rdparty conflicts with FetchContent deps

| OCCT 3rdparty | Our FetchContent dep | Conflict? |
|---------------|---------------------|-----------|
| FreeType      | none                | No        |
| TBB           | none                | No        |
| FreeImage     | none                | No        |
| Tcl/Tk        | none                | No        |
| zlib          | none (stb handles PNG in host separately) | No |
| nlohmann/json | we fetch nlohmann/json 3.11.3 | No — OCCT does NOT use nlohmann/json in its public API; the `RapidJSON` 3rdparty is only for OCCT's glTF reader (not used by us at Wave 2) |
| Eigen         | we fetch Eigen 3.4.0 | No — OCCT does not bundle Eigen; OCCT uses its own math |

**No conflicts.** All of our FetchContent deps (Eigen, doctest, nlohmann/json, GLFW, stb, ImGui)
are orthogonal to the OCCT 3rdparty set.

### 6.6 PATH and DLL search order on Windows

If you launch the test exe from a terminal without the DLL copy having run (e.g., via CMake's
`ctest` before the first build), Windows DLL search will fail silently if the OCCT `bin\` dir is
not in `$env:PATH`. The safest approach is the CMake `POST_BUILD` copy above — it fires on every
rebuild. Alternatively:
```powershell
$env:PATH = "C:\OCCT\opencascade-7.9.3\win64\vc14\bin;" + $env:PATH
```
Do not rely on a global PATH change for CI; use the copy command.

### 6.7 In-process OCCT crash risk (noted in the plan)

Once OCCT is linked into the host process (not just `core/`), a hard crash in a boolean operation
or fillet will take down the GUI. Mitigate:
- Wrap `BRepAlgoAPI_*` and `BRepFilletAPI_*` calls in a `try { ... } catch (Standard_Failure& e)`
  block (OCCT uses C++ exceptions derived from `Standard_Failure`).
- Return a `std::optional<TopoDS_Shape>` with an error string from all feature compilers.
- Keep simulation (`core/sim/`) out-of-process as planned.

---

## 7. CMake Presets (optional but recommended)

Add a `CMakePresets.json` at the repo root to record the OCCT paths so every dev doesn't need to
retype them:

```json
{
  "version": 3,
  "configurePresets": [
    {
      "name": "core-release-occt",
      "displayName": "core Release with OCCT",
      "generator": "Visual Studio 17 2022",
      "architecture": "x64",
      "binaryDir": "${sourceDir}/core/build",
      "cacheVariables": {
        "OCCT_ROOT": "C:/OCCT/opencascade-7.9.3",
        "OCCT_3RDPARTY_ROOT": "C:/OCCT/3rdparty"
      },
      "sourceDir": "${sourceDir}/core"
    }
  ]
}
```

Then configure with: `cmake --preset core-release-occt`

---

## Summary Reference Card

| Item | Value |
|------|-------|
| Recommended version | **OCCT 7.9.3** |
| Primary package | `opencascade-7.9.3-vc14-64-combined.zip` |
| Download | `https://github.com/Open-Cascade-SAS/OCCT/releases/tag/V7_9_3` |
| Extract to | `C:\OCCT\opencascade-7.9.3\` |
| `OpenCASCADE_DIR` | `C:/OCCT/opencascade-7.9.3/cmake` |
| CMake wiring | `-DOCCT_ROOT=C:/OCCT/opencascade-7.9.3` → auto-sets `OpenCASCADE_DIR` |
| License | LGPL-2.1-only WITH OCCT-exception-1.0 — dynamic link is clean |
| Top gotcha | MSVC `/MD` required (default); debug suffix `d`; "vc14" label is VS2022 build |
| Smoke test | `BRepPrimAPI_MakeBox(10,10,10)` + `BRepGProp::VolumeProperties` → 1000 mm³ |
