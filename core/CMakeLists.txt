cmake_minimum_required(VERSION 3.20)

# Product version (semver from annotated git tags). Included BEFORE project() so
# project(VERSION) carries the same numbers the generated header does. Shared
# verbatim with the root CMakeLists.txt: core/ is also configured standalone
# (`cmake -S core -B core/build-occt-linux`), where the root file never runs, and
# two copies of the derive logic would drift.
include("${CMAKE_CURRENT_SOURCE_DIR}/cmake/ChudwareVersion.cmake")
chudware_version_derive()

project(chudware_core VERSION ${CHUDWARE_VERSION} LANGUAGES C CXX)  # C enabled for chudware_miniz (miniz.c); the VS generator
                                     # tolerates an implicit C, but Ninja/Make + GCC/Clang need it explicit.

# The new pure-C++ engine (libchudware). Wave 1 = the constraint solver core; later
# waves add doc/geom (OCCT) and sim (CUDA). Built standalone here so the solver is
# the cleanest possible beachhead — no OCCT, no GUI, no FFI. The host will
# add_subdirectory(core) once a subsystem reaches parity (see docs plan).

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# Build all static libs position-independent so they can be linked into the libchudware
# SHARED library on ELF platforms (Linux/macOS): GCC/Clang reject non-PIC .o in a .so
# ("recompile with -fPIC"). No-op on MSVC (DLLs are inherently relocatable).
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if(MSVC)
  add_compile_options(/utf-8 /MP)
endif()

list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake")

# <build>/generated/api/chudware_version.h. Generated into the BUILD tree rather
# than the source tree so `git status` stays clean and two trees configured from
# the same checkout cannot fight over one file. Adding <build>/generated to a
# target's include path makes `#include "api/chudware_version.h"` resolve exactly
# the way `#include "api/libchudware.h"` resolves from ${CMAKE_CURRENT_SOURCE_DIR}.
chudware_version_header("${CMAKE_BINARY_DIR}/generated")
set(CHUDWARE_GENERATED_INCLUDE_DIR "${CMAKE_BINARY_DIR}/generated")

# Link-time optimization (IPO/LTO). OFF by default: it's a large link-time + memory hit with
# the OCCT-linked libchudware, and normal dev/gate builds don't need it. Turn on for release
# artifacts with -DCHUDWARE_LTO=ON. Guarded by check_ipo_supported so an unsupported toolchain
# degrades to a warning instead of a hard configure error. Only meaningful in a Release build
# (where -O3/-DNDEBUG already apply); a no-op inliner across TU boundaries otherwise.
option(CHUDWARE_LTO "Enable interprocedural (link-time) optimization for release artifacts" OFF)
if(CHUDWARE_LTO)
  # CMP0069 NEW makes INTERPROCEDURAL_OPTIMIZATION actually take effect (else it's silently
  # ignored on targets whose effective policy is OLD); propagate the default to sub-projects.
  if(POLICY CMP0069)
    cmake_policy(SET CMP0069 NEW)
    set(CMAKE_POLICY_DEFAULT_CMP0069 NEW)
  endif()
  include(CheckIPOSupported)
  check_ipo_supported(RESULT _ipo_ok OUTPUT _ipo_msg LANGUAGES CXX)
  if(_ipo_ok)
    set(CMAKE_INTERPROCEDURAL_OPTIMIZATION ON)
    message(STATUS "CHUDWARE_LTO: interprocedural optimization ENABLED")
  else()
    message(WARNING "CHUDWARE_LTO requested but IPO unsupported by this toolchain: ${_ipo_msg}")
  endif()
endif()

# Wave 2: provision + link the OCCT 3D B-rep kernel. OFF keeps the fast solver
# build (Wave 1) self-contained; -DCHUDWARE_WITH_OCCT=ON auto-downloads OCCT and
# builds the geometry kernel + smoke test (see cmake/ProvisionOCCT.cmake).
option(CHUDWARE_WITH_OCCT "Provision + link the OCCT geometry kernel (Wave 2)" OFF)

# MSVC: <windows.h> (pulled in transitively by OCCT headers) defines min()/max() macros that
# clobber std::min / std::max / std::numeric_limits<>::min, breaking e.g. core/api/libchudware.cpp.
# Suppress project-wide. Harmless without OCCT (windows.h isn't included then); mirrors the
# per-file `#define NOMINMAX` already present in a few sources.
if(MSVC)
  add_compile_definitions(NOMINMAX)
endif()

include(FetchContent)

# --- Eigen (header-only linear algebra; replaces numpy/scipy.linalg) ---------
set(BUILD_TESTING OFF CACHE BOOL "" FORCE)
set(EIGEN_BUILD_DOC OFF CACHE BOOL "" FORCE)
set(EIGEN_BUILD_PKGCONFIG OFF CACHE BOOL "" FORCE)
FetchContent_Declare(eigen
  GIT_REPOSITORY https://gitlab.com/libeigen/eigen.git
  GIT_TAG 3.4.0
  GIT_SHALLOW TRUE)
FetchContent_MakeAvailable(eigen)

# --- nlohmann/json (fixture / sketch dict parsing; same lib the host uses) ----
set(JSON_BuildTests OFF CACHE INTERNAL "")
FetchContent_Declare(json
  URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz)
FetchContent_MakeAvailable(json)

# --- miniz (ZIP read+write for the .step+ container) -------------------------
# The amalgamated release ships miniz.c/miniz.h with no CMakeLists at the root, so
# Populate (not MakeAvailable) it and build a tiny static lib ourselves. CMake's
# file(ARCHIVE_EXTRACT) is extract-only — insufficient for save().
FetchContent_Declare(miniz
  URL https://github.com/richgel999/miniz/releases/download/3.0.2/miniz-3.0.2.zip)
FetchContent_GetProperties(miniz)
if(NOT miniz_POPULATED)
  FetchContent_Populate(miniz)
endif()
add_library(chudware_miniz STATIC ${miniz_SOURCE_DIR}/miniz.c)
target_include_directories(chudware_miniz PUBLIC ${miniz_SOURCE_DIR})
# Match the dynamic CRT the rest of the core + the DLL use (silences LNK4098 and keeps
# miniz's malloc/free on the same heap as the DLL that calls mz_free).
set_property(TARGET chudware_miniz PROPERTY
  MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")

# --- Spectra (header-only eigensolvers over Eigen; modal FEA shift-invert) ----
# Like doctest, point SOURCE_SUBDIR at a non-existent subdir so MakeAvailable only
# POPULATES it (we add ${spectra_SOURCE_DIR}/include to the path) and skips Spectra's
# own CMake (avoids any cmake_minimum_required friction under CMake 4.x).
FetchContent_Declare(spectra
  GIT_REPOSITORY https://github.com/yixuan/spectra.git
  GIT_TAG v1.0.1
  GIT_SHALLOW TRUE
  SOURCE_SUBDIR _no_cmake_)
FetchContent_MakeAvailable(spectra)

# --- doctest (C++ unit tests; header-only) -----------------------------------
# Point SOURCE_SUBDIR at the header folder (no CMakeLists there) so MakeAvailable
# populates but skips doctest's own ancient cmake_minimum_required(3.0), which
# errors under CMake 4.x. We just add the header dir to the include path.
FetchContent_Declare(doctest
  GIT_REPOSITORY https://github.com/doctest/doctest.git
  GIT_TAG v2.4.11
  GIT_SHALLOW TRUE
  SOURCE_SUBDIR doctest)
FetchContent_MakeAvailable(doctest)

# --- Clipper2 (polygon offset / boolean for the CAM toolpath engine) ----------
# Boost Software License 1.0 (GPL-clean, commercial-safe). The amalgamated lib
# ships its own CMakeLists under CPP/, but we POPULATE-only and compile the three
# engine .cpp ourselves into a tiny static lib (mirrors the miniz approach) to
# skip Clipper2's tests/benchmarks/USINGZ options. See docs/contracts/licenses.md.
FetchContent_Declare(clipper2
  GIT_REPOSITORY https://github.com/AngusJohnson/Clipper2.git
  GIT_TAG Clipper2_1.5.4
  GIT_SHALLOW TRUE
  SOURCE_SUBDIR _no_cmake_)
FetchContent_MakeAvailable(clipper2)
add_library(chudware_clipper2 STATIC
  ${clipper2_SOURCE_DIR}/CPP/Clipper2Lib/src/clipper.engine.cpp
  ${clipper2_SOURCE_DIR}/CPP/Clipper2Lib/src/clipper.offset.cpp
  ${clipper2_SOURCE_DIR}/CPP/Clipper2Lib/src/clipper.rectclip.cpp)
target_include_directories(chudware_clipper2 PUBLIC
  ${clipper2_SOURCE_DIR}/CPP/Clipper2Lib/include)
set_property(TARGET chudware_clipper2 PROPERTY
  MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")

# --- chudware_prof: in-process sampling profiler (Windows + Linux; no-op elsewhere) ----
# Windows: periodically suspends the registered modeling thread and walks its stack lock-free
# (RtlVirtualUnwind), symbolizing with DbgHelp at report time. Linux: a sampler thread signals
# the work thread, an async-signal-safe backtrace() feeds a ring, and dladdr/__cxa_demangle
# symbolize at report time. OCCT-free + tiny, so it links cleanly into libchudware.{dll,so} and the
# unit test. See core/prof/sampler.{h,cpp} and docs/PROFILER.md.
add_library(chudware_prof STATIC
  prof/sampler.cpp
  prof/phases.cpp
  prof/tree.cpp)
target_include_directories(chudware_prof PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_prof PUBLIC nlohmann_json::nlohmann_json)
find_package(Threads REQUIRED)
if(WIN32)
  target_link_libraries(chudware_prof PUBLIC dbghelp)
else()
  # Linux/Unix: dladdr (symbolization) is in libdl on older glibc (CMAKE_DL_LIBS -> "dl" or "");
  # the sampler/drain run on a std::thread and the handler uses pthread_sigmask, so the POSIX
  # threads lib is required too. PUBLIC so both land on every consumer's final link line
  # (libchudware.so + test_prof).
  target_link_libraries(chudware_prof PUBLIC ${CMAKE_DL_LIBS} Threads::Threads)
endif()
set_property(TARGET chudware_prof PROPERTY
  MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")

# --- libchudware solver ---------------------------------------------------------
add_library(chudware_solver STATIC
  solver/nsolve.cpp)
target_include_directories(chudware_solver PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_solver PUBLIC Eigen3::Eigen)

# --- libchudware geom (sketch-plane 2D<->3D lift + polar helpers) ---------------
add_library(chudware_geom STATIC
  geom/plane.cpp
  geom/airfoil.cpp)
target_include_directories(chudware_geom PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_geom PUBLIC Eigen3::Eigen)

# --- libchudware util (dimension-expression arithmetic evaluator + AICM handoff) -
# C++ port of engine/doc/equations.py (the `evaluate` entry point) so the sketch
# solver's resolve_dim() matches the Python oracle for expr:"A*B"-style dims.
# aicm_handoff: POSIX shm / Win named mapping for tess mesh (T-PERF10).
# platform_paths / parent_death: the one spelling per OS of "where am I", "is that
# pid alive", and "bind this child's life to mine" — see util/platform_paths.h.
add_library(chudware_util STATIC
  util/expr.cpp
  util/aicm_handoff.cpp
  util/resource_governor.cpp
  util/thread_pool.cpp
  util/platform_paths.cpp
  util/parent_death.cpp)
target_include_directories(chudware_util PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_util PUBLIC Threads::Threads)
if(UNIX AND NOT APPLE)
  # shm_open/shm_unlink live in librt on older glibc; harmless empty on new glibc.
  target_link_libraries(chudware_util PUBLIC rt)
endif()

# --- libchudware sim (simulation solvers) ---------------------------------------
# C++ port of engine/sim. FEA static linear elasticity first (tet4 + Eigen::SparseLU,
# the oracle is engine/sim/fea_static.py); modal / LBM / SPICE follow. OCCT-free —
# pure Eigen — so it builds in the standalone config too. CUDA acceleration is a
# later layer on top of these correct CPU cores.
add_library(chudware_sim STATIC
  sim/fea_static.cpp
  sim/fea_modal.cpp
  sim/fea_thermal.cpp
  sim/spice.cpp
  sim/numpy_rng.cpp
  sim/netlist.cpp
  sim/lbm.cpp
  sim/flow_run.cpp
  sim/sim_cache.cpp
  sim/sim_cancel.cpp
  sim/spice_sweep.cpp)
target_include_directories(chudware_sim PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_include_directories(chudware_sim PRIVATE ${spectra_SOURCE_DIR}/include)
# chudware_util: host resource_governor (memory admission/degrade). chudware_prof: the per-phase
# sim timers (prof/phases.h) every solver records into. PUBLIC so chudware_mesh (fea_run/mesher)
# and the final chudware DLL inherit both links.
target_link_libraries(chudware_sim PUBLIC Eigen3::Eigen nlohmann_json::nlohmann_json chudware_util chudware_prof)

# --- Optional CHOLMOD (SuiteSparse) supernodal Cholesky for the SPD FEA solve ------
# Eigen's SimplicialLDLT is scalar + single-threaded; on a bulky 3D solid its fill-in
# explodes (measured: 300s+, multi-GB RSS, one core pegged). CholmodSupernodalLLT runs
# BLAS3 kernels on dense supernodes over the threaded system BLAS -> 10-50x faster AND
# multithreaded. OPTIONAL: chudware_sim still builds pure-Eigen when SuiteSparse is absent
# (fea_static falls back to SimplicialLDLT at runtime), so the standalone config is intact.
# OpenMP is linked so fea_static can cap the solver's thread count (leave cores for the GUI).
option(CHUDWARE_WITH_CHOLMOD "Use SuiteSparse CHOLMOD supernodal Cholesky for FEA (SPD)" ON)
if(CHUDWARE_WITH_CHOLMOD)
  find_path(CHOLMOD_INCLUDE_DIR cholmod.h PATH_SUFFIXES suitesparse)
  find_library(CHOLMOD_LIBRARY cholmod)
  find_library(SUITESPARSECONFIG_LIBRARY suitesparseconfig)
  if(CHOLMOD_INCLUDE_DIR AND CHOLMOD_LIBRARY)
    message(STATUS "chudware_sim: CHOLMOD supernodal FEA solver ENABLED (${CHOLMOD_LIBRARY})")
    target_compile_definitions(chudware_sim PRIVATE CHUDWARE_WITH_CHOLMOD)
    target_include_directories(chudware_sim PRIVATE ${CHOLMOD_INCLUDE_DIR})
    target_link_libraries(chudware_sim PUBLIC ${CHOLMOD_LIBRARY})
    if(SUITESPARSECONFIG_LIBRARY)
      target_link_libraries(chudware_sim PUBLIC ${SUITESPARSECONFIG_LIBRARY})
    endif()
  else()
    message(STATUS "chudware_sim: CHOLMOD not found — FEA uses Eigen SimplicialLDLT fallback")
  endif()
endif()
find_package(OpenMP QUIET)
if(OpenMP_CXX_FOUND)
  target_link_libraries(chudware_sim PRIVATE OpenMP::OpenMP_CXX)
  target_compile_definitions(chudware_sim PRIVATE CHUDWARE_HAVE_OPENMP)
endif()


# --- Optional CUDA LBM backend (D3Q19 collide/stream/BC) -------------------------
# Default OFF so CPU-only builds stay unchanged. When ON, finds CUDAToolkit at the
# CUDA 13.3 toolkit path if present and compiles sim/lbm_cuda.cu into chudware_sim.
option(CHUDWARE_WITH_CUDA "Build optional CUDA LBM backend (requires CUDAToolkit 13+)" OFF)
if(CHUDWARE_WITH_CUDA)
  if(NOT DEFINED CMAKE_CUDA_COMPILER AND EXISTS "/usr/local/cuda-13.3/bin/nvcc")
    set(CMAKE_CUDA_COMPILER "/usr/local/cuda-13.3/bin/nvcc" CACHE FILEPATH "nvcc" FORCE)
  endif()
  enable_language(CUDA)
  find_package(CUDAToolkit REQUIRED)
  message(STATUS "chudware_sim: CUDA LBM backend ENABLED (${CUDAToolkit_VERSION})")
  target_sources(chudware_sim PRIVATE sim/lbm_cuda.cu)
  target_compile_definitions(chudware_sim PUBLIC CHUDWARE_WITH_CUDA)
  target_link_libraries(chudware_sim PUBLIC CUDA::cudart)
  set_target_properties(chudware_sim PROPERTIES
    CUDA_SEPARABLE_COMPILATION ON
    CUDA_ARCHITECTURES "75;86"
    CUDA_STANDARD 17)
  # sm_75 (Turing) + sm_86 (Ampere, RTX 3080 Ti). allow-unsupported-compiler is
  # needed when the host GCC is newer than nvcc's official support ceiling.
  target_compile_options(chudware_sim PRIVATE
    $<$<COMPILE_LANGUAGE:CUDA>:-allow-unsupported-compiler>)
endif()


# --- libchudware cam (computer-aided machining: toolpaths + post + sim) ----------
# The CAM suite: stock/tool/machine/op model, Clipper2-backed 2D geometry, the
# 2.5D/3D/turning/cutting toolpath generators, feeds & speeds, the data-driven
# post-processor (GRBL/LinuxCNC/Fanuc/Haas/Mach3/Marlin/Siemens/Centroid), the
# material-removal sim, and the stateful job session. OCCT-FREE — pure numerics +
# JSON like chudware_sim/chudware_ecad, so it builds in the standalone config and is
# unit-tested headless (test_cam). A separate OCCT extraction adapter (geom layer)
# feeds live-part geometry into operations for the product. See docs/CAM_PLAN.md.
add_library(chudware_cam STATIC
  cam/model.cpp
  cam/geometry.cpp
  cam/feeds.cpp
  cam/toolpath.cpp
  cam/operations.cpp
  cam/op_milling.cpp
  cam/op_drill.cpp
  cam/op_engrave.cpp
  cam/op_adaptive.cpp  cam/heightfield.cpp
  cam/op_3d.cpp
  cam/op_cut.cpp
  cam/op_turn.cpp
  cam/post.cpp
  cam/stock_sim.cpp
  cam/audit.cpp
  cam/session.cpp
  cam/turn/process.cpp              # single-spindle XZ turning process model
  cam/cut/process.cpp)              # laser / plasma / waterjet process profiles
target_include_directories(chudware_cam PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_cam PUBLIC
  chudware_clipper2 chudware_miniz nlohmann_json::nlohmann_json)
# MSVC only defines M_PI et al. in <cmath> when _USE_MATH_DEFINES is set; the CAM math
# (helix/adaptive/stock-sim) relies on them. GCC/Clang expose them by default, so this is
# a no-op there. Target-scoped to avoid redefinition warnings in files that set it locally.
if(MSVC)
  # PUBLIC so consumers (e.g. test_cam) that use M_PI via the CAM headers get it too.
  target_compile_definitions(chudware_cam PUBLIC _USE_MATH_DEFINES)
endif()
set_property(TARGET chudware_cam PROPERTY
  MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")

# --- libchudware ecad (ECAD document model: schema + ops + session) -------------
# C++ port of engine/ecad (schema.py + ops.py + session.py). OCCT-free — the
# ECAD doc/ops layer is pure JSON (like chudware_sim) — so it builds in the
# standalone config and stays fast + isolated. Netlist/ERC/DRC/autoroute/Gerber/
# 3D are DEFERRED to later waves.
add_library(chudware_ecad STATIC
  ecad/schema.cpp
  ecad/ops.cpp
  ecad/op_registry.cpp
  ecad/netlist.cpp
  ecad/erc.cpp
  ecad/drc.cpp
  ecad/autoroute.cpp
  ecad/dfm.cpp
  ecad/impedance.cpp
  ecad/zonefill.cpp
  ecad/fabformats.cpp
  ecad/library.cpp
  ecad/recipes.cpp
  ecad/sync.cpp
  ecad/container.cpp
  ecad/kicad.cpp
  ecad/session.cpp)
target_include_directories(chudware_ecad PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
# chudware_miniz is the ZIP read/write lib (shared with the .step+ container) — the
# .ecad+ container (container.cpp) writes/reads its archive through it.
# chudware_sim provides the SPICE circuit builder + MNA simulator that ecad_simulate
# (session.cpp) drives (schematic netlist -> SPICE circuit -> run). chudware_sim is
# pure-JSON and does NOT depend on chudware_ecad, so this introduces no cycle.
target_link_libraries(chudware_ecad PUBLIC nlohmann_json::nlohmann_json chudware_miniz chudware_sim)

# mega-28 E28: autorouter min-clearance (pad/track spacing) — pure JSON, OCCT-free.
add_executable(test_autoroute tests/test_autoroute.cpp)
target_include_directories(test_autoroute PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_autoroute PRIVATE chudware_ecad)
set_target_properties(test_autoroute PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME autoroute COMMAND test_autoroute)

# T-RF15 (node R4a): firmware engine surface — project scaffold + pin-map +
# toolchain/build wrappers + probe-rs/esptool hardware path + co-verification +
# the Renode/QEMU emulator leg (fw_sim_run incl. §7 trace + §9 scenarios,
# fw_power_profile, fw_sensor_scenario) + the fw_* RPC dispatch
# (firmware/firmware_rpc.cpp). Pure JSON like chudware_sim/chudware_ecad; links
# chudware_ecad for session/netlist/schema/container resolution of board_doc_ref.
add_library(chudware_firmware STATIC
  firmware/subprocess.cpp
  firmware/templates_embedded.cpp
  firmware/mcu_pinouts.cpp
  firmware/pinmap.cpp
  firmware/project.cpp
  firmware/toolchains.cpp
  firmware/hardware.cpp
  firmware/platform_db.cpp
  firmware/provision.cpp
  firmware/coverify.cpp
  firmware/eng_rails.cpp
  firmware/pyfmt.cpp
  firmware/scenario.cpp
  firmware/emulator.cpp
  firmware/powerprofile.cpp
  firmware/sensor_models.cpp
  firmware/sim_sensor_bridge.cpp
  firmware/firmware_rpc.cpp)
target_include_directories(chudware_firmware PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_firmware PUBLIC nlohmann_json::nlohmann_json chudware_ecad)
if(MSVC)
  # MSVC only defines M_PI et al. in <cmath> when _USE_MATH_DEFINES is set (firmware/scenario.cpp).
  target_compile_definitions(chudware_firmware PUBLIC _USE_MATH_DEFINES)
endif()

add_executable(test_firmware tests/test_firmware.cpp)
target_include_directories(test_firmware PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_firmware PRIVATE chudware_firmware)
# The fw_* tests read small ECAD board/schematic/trace fixtures from the source
# tree; pass the absolute dir so the test runs from the build dir.
target_compile_definitions(test_firmware PRIVATE
  CHUDWARE_TEST_DATA="${CMAKE_CURRENT_SOURCE_DIR}/tests/data/firmware")
set_target_properties(test_firmware PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME firmware COMMAND test_firmware)
# RUN_SERIAL + a generous TIMEOUT. The live legs launch real renode/qemu processes whose
# cold start is CPU-bound (~10-20 s on an idle box). Under `ctest -j6` the Netgen mesher
# suites (mesher_autopick alone is ~100 s of saturated cores) starved renode past its
# 15/20 s per-run budget, so `firmware` failed only in PARALLEL runs and passed in
# isolation — a pure machine-load artifact, not a firmware defect.
#
# Both halves were measured over 3 consecutive `ctest -j6` runs each:
#   90 s budgets only (no RUN_SERIAL) -> 1 of 3 runs still FAILED, ~178 s per run.
#   90 s budgets + RUN_SERIAL         -> 3 of 3 PASSED, ~268 s per run.
# So serialization is load-bearing, not belt-and-braces. The ~90 s of extra wall time
# buys a deterministic gate; a gate that fails a third of the time is worth nothing.
# Do not drop RUN_SERIAL without re-running that 3x comparison.
set_tests_properties(firmware PROPERTIES RUN_SERIAL TRUE TIMEOUT 600)

# fw_cli: differential-harness driver (same role as sketch_cli) — reads
# {"method","params"} and prints the firmware::call result as one JSON line.
# Used by the T-RF15 parity diffs against the Python oracle.
add_executable(fw_cli firmware/fw_cli.cpp)
target_link_libraries(fw_cli PRIVATE chudware_firmware)
set_target_properties(fw_cli PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# T-RF17: KiCad importers (import_kicad_sch / import_kicad_pcb) + the ecad_open
# .kicad_* extension dispatch — mirrors engine/ecad/test_formats.py tests 3-6.
add_executable(test_kicad_import tests/test_kicad_import.cpp)
target_include_directories(test_kicad_import PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_kicad_import PRIVATE chudware_ecad)
set_target_properties(test_kicad_import PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME kicad_import COMMAND test_kicad_import)

# T-RF19 (leaf 5): structural engine surface — aicad.structure/1 document model,
# the direct-stiffness 3D frame solver (PyNite-equivalent subset: global-
# direction point/distributed member loads, fully-fixed supports, ASCE 7
# combos, beam-segmentation member-force/deflection extraction), the AISC +
# lumber section libraries (embedded curated tables), the AISC-LRFD / NDS-ASD
# D/C checks, the ENR-rate cost takeoff, and the struct_* RPC dispatch
# (structural/structural_rpc.cpp). Pure Eigen + JSON like chudware_sim; OCCT-free.
add_library(chudware_structural STATIC
  structural/model.cpp
  structural/solver.cpp
  structural/sections.cpp
  structural/checks.cpp
  structural/estimate.cpp
  structural/structural_rpc.cpp)
target_include_directories(chudware_structural PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_structural PUBLIC Eigen3::Eigen nlohmann_json::nlohmann_json)

add_executable(test_structural tests/test_structural.cpp)
target_include_directories(test_structural PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_structural PRIVATE chudware_structural)
# The structural tests read aicad.structure/1 fixtures + the ENR rate table
# from the source tree; pass the absolute dirs so the test runs from the build dir.
target_compile_definitions(test_structural PRIVATE
  CHUDWARE_TEST_DATA="${CMAKE_CURRENT_SOURCE_DIR}/tests/data/structural"
  CHUDWARE_STRUCT_DATA="${CMAKE_CURRENT_SOURCE_DIR}/structural")
set_target_properties(test_structural PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME structural COMMAND test_structural)

# struct_cli: differential-harness driver (same role as fw_cli) — reads
# {"method","params"} and prints the structural::call result as one JSON line.
# Used by the T-RF19 parity diffs against the Python (PyNite) oracle.
add_executable(struct_cli structural/struct_cli.cpp)
target_link_libraries(struct_cli PRIVATE chudware_structural)
set_target_properties(struct_cli PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

enable_testing()

add_executable(test_sim tests/test_sim.cpp)
target_include_directories(test_sim PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_sim PRIVATE chudware_sim)
set_target_properties(test_sim PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME sim COMMAND test_sim)

# Second-order (tet10) quadratic-tet FEA gate: node-ordering sanity + axial accuracy
# + cantilever bending (tet10 relieves tet4 shear-locking). OCCT-/gmsh-free (inline
# structured box meshes), so it links only chudware_sim like test_sim.
add_executable(test_modal tests/test_modal.cpp)
target_include_directories(test_modal PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_modal PRIVATE chudware_sim)
set_target_properties(test_modal PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME modal COMMAND test_modal)

add_executable(test_tet10 tests/test_tet10.cpp)
target_include_directories(test_tet10 PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_tet10 PRIVATE chudware_sim)
set_target_properties(test_tet10 PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME tet10 COMMAND test_tet10)

# T-SIM1: steady-state thermal (heat-conduction) FEA gate — validates the conduction
# kernel against the closed-form 1D plane-wall (Dirichlet) and wall+convection (Robin)
# solutions, on tet4 and tet10. OCCT-/mesher-free (inline structured box meshes), so it
# links only chudware_sim like test_sim / test_modal.
add_executable(test_thermal tests/test_thermal.cpp)
target_include_directories(test_thermal PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_thermal PRIVATE chudware_sim)
set_target_properties(test_thermal PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME thermal COMMAND test_thermal)

# SPICE deck parser + schematic->circuit mapper (parse_netlist / netlist_to_spice_circuit,
# incl. the starter SYMBOL libid fallback via symbol_sim_table). OCCT-free, links chudware_sim.
add_executable(test_spice_netlist
  tests/test_spice_netlist.cpp)
target_include_directories(test_spice_netlist PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_spice_netlist PRIVATE chudware_sim)
set_target_properties(test_spice_netlist PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME spice_netlist COMMAND test_spice_netlist)

# --- batched parallel SPICE sweep (governed thread pool, OCCT-free) ----------
add_executable(test_spice_sweep
  tests/test_spice_sweep.cpp)
target_include_directories(test_spice_sweep PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_spice_sweep PRIVATE chudware_sim)
set_target_properties(test_spice_sweep PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME spice_sweep COMMAND test_spice_sweep)

# CAM suite tests: model serialization round-trips, Clipper2 geometry (offset /
# boolean / pocket clearing / raster), feeds & speeds, the toolpath generators,
# the post-processor dialects, and the material-removal sim. OCCT-free; links
# chudware_cam (which pulls in Clipper2 + miniz). Grows with each CAM wave.
add_executable(test_cam
  tests/test_cam.cpp)
target_include_directories(test_cam PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_cam PRIVATE chudware_cam)
# T-M4: the byte-identical feeds test reads the source-tree registry.json (the CAM fold-in
# ground truth); pass its absolute path so the test runs from the build dir.
target_compile_definitions(test_cam PRIVATE
  CHUDWARE_TEST_REGISTRY="${CMAKE_CURRENT_SOURCE_DIR}/data/materials/registry.json"
  CHUDWARE_TEST_MACHINES="${CMAKE_CURRENT_SOURCE_DIR}/../services/research/data/machines.json")
set_target_properties(test_cam PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME cam COMMAND test_cam)

# --- libchudware sketch (parameterization + constraints over the solver core) ---
add_library(chudware_sketch STATIC
  sketch/sketch.cpp
  sketch/sketch3d.cpp)
target_include_directories(chudware_sketch PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_sketch PUBLIC chudware_solver chudware_util nlohmann_json::nlohmann_json)

# --- sketch_cli (differential-harness driver; see tools/solver_diff.py) -------
# Reads { "sketch":{...}, "env":{...} } from argv[1] (or stdin if "-") and prints
# the solve_sketch result as one JSON line. Output to bin/ alongside the tests.
add_executable(sketch_cli
  sketch/sketch_cli.cpp)
target_link_libraries(sketch_cli PRIVATE chudware_sketch)
set_target_properties(sketch_cli PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# --- tests -------------------------------------------------------------------
add_executable(test_nsolve
  tests/test_nsolve.cpp)
target_include_directories(test_nsolve PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_nsolve PRIVATE chudware_solver)
set_target_properties(test_nsolve PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME nsolve COMMAND test_nsolve)

# --- sampling profiler unit test (OCCT-free) ---------------------------------
# Windows: ships a PDB (/Zi /DEBUG) so the hot function resolves by name. Linux: exports its
# symbols (-rdynamic) so dladdr can name frames. Either way it exercises the stack walk +
# symbolization end to end.
add_executable(test_prof
  tests/test_prof.cpp)
target_include_directories(test_prof PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_prof PRIVATE chudware_prof)
set_target_properties(test_prof PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
if(MSVC)
  target_compile_options(test_prof PRIVATE /Zi)
  target_link_options(test_prof PRIVATE /DEBUG /OPT:REF /OPT:ICF)
elseif(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  # Export the executable's symbols into the dynamic symbol table so dladdr() (Linux
  # symbolization) can name our frames in the report.
  target_link_options(test_prof PRIVATE -rdynamic)
endif()
add_test(NAME prof COMMAND test_prof)

# --- per-phase sim timer unit test (OCCT-free) --------------------------------
# Exercises prof/phases.{h,cpp} directly (StudyScope/PhaseScope/add/report) AND proves the
# fea_static solver records its solve.* phases end to end (chudware_sim is OCCT-free).
add_executable(test_prof_phases
  tests/test_prof_phases.cpp)
target_include_directories(test_prof_phases PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_prof_phases PRIVATE chudware_prof chudware_sim)
set_target_properties(test_prof_phases PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME prof_phases COMMAND test_prof_phases)

# --- nested call-tree / phase-tree profiler unit test (OCCT-free) ------------
add_executable(test_prof_tree
  tests/test_prof_tree.cpp)
target_include_directories(test_prof_tree PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_prof_tree PRIVATE chudware_prof)
set_target_properties(test_prof_tree PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME prof_tree COMMAND test_prof_tree)

add_executable(test_sketch
  tests/test_sketch.cpp)
target_include_directories(test_sketch PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_sketch PRIVATE chudware_sketch)
set_target_properties(test_sketch PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME sketch COMMAND test_sketch)

add_executable(test_sketch3d
  tests/test_sketch3d.cpp)
target_include_directories(test_sketch3d PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_sketch3d PRIVATE chudware_sketch)
set_target_properties(test_sketch3d PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME sketch3d COMMAND test_sketch3d)

add_executable(test_drag
  tests/test_drag.cpp)
target_include_directories(test_drag PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_drag PRIVATE chudware_sketch)
set_target_properties(test_drag PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME drag COMMAND test_drag)

add_executable(test_freecad_parity
  tests/test_freecad_parity.cpp)
target_include_directories(test_freecad_parity PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_freecad_parity PRIVATE chudware_sketch)
set_target_properties(test_freecad_parity PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME freecad_parity COMMAND test_freecad_parity)

add_executable(test_plane
  tests/test_plane.cpp)
target_include_directories(test_plane PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_plane PRIVATE chudware_geom)
set_target_properties(test_plane PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME plane COMMAND test_plane)

add_executable(test_expr
  tests/test_expr.cpp)
target_include_directories(test_expr PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_expr PRIVATE chudware_util)
set_target_properties(test_expr PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME expr COMMAND test_expr)

add_executable(test_governor
  tests/test_governor.cpp)
target_include_directories(test_governor PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_governor PRIVATE chudware_util)
set_target_properties(test_governor PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME governor COMMAND test_governor)

# --- shared thread-pool unit test (governed concurrency, OCCT-free) ----------
add_executable(test_thread_pool
  tests/test_thread_pool.cpp)
target_include_directories(test_thread_pool PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_thread_pool PRIVATE chudware_util)
set_target_properties(test_thread_pool PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME thread_pool COMMAND test_thread_pool)

# --- libchudware geom-aicm (OCCT-free AICM v1 writer) ---------------------------
# The encoder half of engine/doc/tessellate.py, kept OCCT-free so it builds and
# unit-tests in the default (CHUDWARE_WITH_OCCT=OFF) configuration too. The OCCT
# tessellator below links this same lib.
add_library(chudware_aicm STATIC
  geom/aicm.cpp)
target_include_directories(chudware_aicm PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})

# --- libchudware agent (LLM provider layer; WinHTTP TLS, OCCT-free) -------------
# P51: C++ port of agent/providers/*.py — the request builders + response parsers
# for Anthropic / OpenAI-compatible / llama.cpp / mock providers, plus a native
# WinHTTP POST (no libcurl/OpenSSL). Top-level (NOT inside if(CHUDWARE_WITH_OCCT)) so
# it builds in both configs. Dynamic CRT to match chudware_miniz / the DLL heap.
add_library(chudware_agent STATIC
  agent/http.cpp
  agent/omp_bridge_client.cpp
  agent/auth.cpp
  agent/providers.cpp
  agent/paths.cpp
  agent/config.cpp
  agent/copilot_modes.cpp
  agent/ops_emit.cpp
  agent/events.cpp
  agent/roles.cpp
  agent/state.cpp
  agent/machine.cpp
  agent/run.cpp
  agent/run_stop.cpp
  agent/micro.cpp
  agent/ask.cpp
  agent/clarify.cpp
  agent/complexity.cpp
  agent/domain.cpp
  agent/firm_bid.cpp
  agent/firm_team.cpp
  agent/iface_plan.cpp
  agent/pm_assist.cpp)
target_include_directories(chudware_agent PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
# chudware_util: platform_paths (executablePath) + parent_death (fork/exec child
# hardening) for omp_bridge_client.cpp and run.cpp's worker spawn.
target_link_libraries(chudware_agent PUBLIC nlohmann_json::nlohmann_json chudware_util)
if(WIN32)
  target_link_libraries(chudware_agent PUBLIC winhttp bcrypt)
else()
  # POSIX HTTP backend (agent/http.cpp #else branch): libcurl over the system TLS/CA store.
  find_package(CURL REQUIRED)
  target_link_libraries(chudware_agent PUBLIC CURL::libcurl)
endif()
set_property(TARGET chudware_agent PROPERTY
  MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")

add_executable(test_agent
  tests/test_agent.cpp)
target_include_directories(test_agent PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_agent PRIVATE chudware_agent)
set_target_properties(test_agent PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
# test_agent loads role prompts from the repo-relative agent/prompts/ — run it from
# the repo root (core/..) so the lookup resolves regardless of where ctest is invoked.
add_test(NAME agent COMMAND test_agent WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/..)

# subscription-auth store + pre-run refresh (agent/auth.cpp). OCCT-free, links
# chudware_agent; all cases run against a scratch XDG config dir with a fake HTTP seam.
add_executable(test_agent_auth
  tests/test_agent_auth.cpp)
target_include_directories(test_agent_auth PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_agent_auth PRIVATE chudware_agent)
set_target_properties(test_agent_auth PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME agent_auth COMMAND test_agent_auth)

# err_sig identical-failure detector (agent/machine.cpp). OCCT-free, links chudware_agent.
add_executable(test_machine
  tests/test_machine.cpp)
target_include_directories(test_machine PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_machine PRIVATE chudware_agent)
set_target_properties(test_machine PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME machine COMMAND test_machine)

# complexity router (agent/complexity.cpp). OCCT-free, links chudware_agent.
add_executable(test_complexity
  tests/test_complexity.cpp)
target_include_directories(test_complexity PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_complexity PRIVATE chudware_agent)
set_target_properties(test_complexity PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME complexity COMMAND test_complexity)

# Copilot mode tool-gating contract (agent/copilot_modes.cpp ←
# contracts/copilot_modes.json). OCCT-free, links chudware_agent. Runs from the
# repo root (like test_agent) so the repo-relative contracts/ fallback resolves.
add_executable(test_copilot_modes
  tests/test_copilot_modes.cpp)
target_include_directories(test_copilot_modes PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_copilot_modes PRIVATE chudware_agent)
set_target_properties(test_copilot_modes PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME copilot_modes COMMAND test_copilot_modes
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/..)

# domain classifier + tool-class registry (agent/domain.cpp). OCCT-free.
add_executable(test_domain
  tests/test_domain.cpp)
target_include_directories(test_domain PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_domain PRIVATE chudware_agent)
set_target_properties(test_domain PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME domain COMMAND test_domain)

# firm bid estimator + roster (agent/firm_bid.cpp). OCCT-free.
add_executable(test_firm_bid
  tests/test_firm_bid.cpp)
target_include_directories(test_firm_bid PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_firm_bid PRIVATE chudware_agent)
set_target_properties(test_firm_bid PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME firm_bid COMMAND test_firm_bid)

# firm team assembly + specialist roles (agent/firm_team.cpp). OCCT-free.
add_executable(test_firm_team
  tests/test_firm_team.cpp)
target_include_directories(test_firm_team PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_firm_team PRIVATE chudware_agent)
set_target_properties(test_firm_team PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME firm_team COMMAND test_firm_team)

add_executable(test_aicm
  tests/test_aicm_writer.cpp)
# Include the host loader's OWN directory (host/src/render), NOT host/src — the
# latter contains a project-local math.h that shadows the CRT <math.h> and breaks
# the standard <cmath>/<cstdlib> headers (MSVC C2039 'sinhf' etc.).
target_include_directories(test_aicm PRIVATE
  ${doctest_SOURCE_DIR}
  ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render)
target_link_libraries(test_aicm PRIVATE chudware_aicm chudware_util)
target_sources(test_aicm PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render/aicm_loader.cpp)
set_target_properties(test_aicm PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME aicm COMMAND test_aicm)

# --- libchudware parts (parametric standard-parts catalog) ----------------------
# Generates EDITABLE geometry for off-the-shelf hardware (fasteners/nuts/washers/
# standoffs/bearings/inserts/dowels) by EMITTING document ops — so it is OCCT-free
# (only JSON; the doc engine compiles the geometry). The numbers mirror
# agent/prompts/shared/cheatsheets/hardware.md. Linked into the libchudware DLL below
# (parts_make / parts_catalog_list RPCs) and unit-tested in both configs.
# parts/place.cpp (T-RF19 place.py port) is the part_place / part_import_file
# placement router: the symbol_footprint leg drives chudware_ecad's apply_ops and
# the fitting_id / fmu_component legs use the chudware_piping / chudware_hydraulic
# schema factories — all still pure JSON / OCCT-free (the doc-creating RPCs and
# the PartRecord hub lookup stay Seams injections wired in libchudware.cpp).
add_library(chudware_parts STATIC
  parts/catalog.cpp
  parts/generate.cpp
  parts/place.cpp)
target_include_directories(chudware_parts PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_parts PUBLIC
  nlohmann_json::nlohmann_json chudware_ecad chudware_hydraulic)

add_executable(test_parts tests/test_parts.cpp)
target_include_directories(test_parts PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_parts PRIVATE chudware_parts)
set_target_properties(test_parts PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME parts COMMAND test_parts)

add_executable(test_parts_place tests/test_parts_place.cpp)
target_include_directories(test_parts_place PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_parts_place PRIVATE chudware_parts)
set_target_properties(test_parts_place PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME parts_place COMMAND test_parts_place)

# --- libchudware sourcing (BOM roll-up / cost model / spec-match / providers) -------
# T-RF18: C++ port of engine_oracle/{bom,cost_model,spec_sheet_matcher,parts_providers,
# quote_providers}.py backing the worker methods bom_rollup / bom_price /
# request_quote / match_part_number / vendor_part_fetch and the MCP spec_sheet_match
# lane. PURE (OCCT-free, no doc/ecad/agent linkage): every side effect — assembly /
# board loading, HTTP transport, model building — is a Seams injection point, so the
# libchudware dispatch glue supplies the production defaults and tests run fully
# offline. Linked into the libchudware DLL below and unit-tested in both configs.
add_library(chudware_sourcing STATIC
  sourcing/bom.cpp
  sourcing/config.cpp
  sourcing/cost.cpp
  sourcing/providers.cpp
  sourcing/quote.cpp
  sourcing/sourcing.cpp
  sourcing/specmatch.cpp
  sourcing/vendorreg.cpp)
target_include_directories(chudware_sourcing PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_sourcing PUBLIC nlohmann_json::nlohmann_json)

add_executable(test_sourcing tests/test_sourcing.cpp)
target_include_directories(test_sourcing PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_sourcing PRIVATE chudware_sourcing)
set_target_properties(test_sourcing PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME sourcing COMMAND test_sourcing)

# --- libchudware tolerance / mechanical / materials / dfm (T-RF19 leaf 6) ------------
# C++ ports of engine_oracle/{tolerance/stackup, mechanical/{gear_check,bearing_life},
# materials/registry, dfm/process_profiles}.py — the small pure oracle modules behind
# the MCP stackup_analyze / gear_check / bearing_life tools, the CAM materials seam,
# the FEA material library and the intake/dimension-scheme DfM brain. PURE (OCCT-free,
# no doc/ecad/agent linkage); each owns its methods behind a single
# call(method, params, result, err) dispatch entry for the libchudware glue. Envelope
# parity with the Python oracle is proven by golden fixtures under tests/data/
# (generated from the oracle — see each test's header comment).
add_library(chudware_tolerance STATIC
  tolerance/stackup.cpp)
target_include_directories(chudware_tolerance PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_tolerance PUBLIC nlohmann_json::nlohmann_json)

add_library(chudware_mechanical STATIC
  mechanical/mechanical.cpp)
target_include_directories(chudware_mechanical PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_mechanical PUBLIC nlohmann_json::nlohmann_json)

add_library(chudware_materials STATIC
  materials/materials.cpp)
target_include_directories(chudware_materials PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_materials PUBLIC nlohmann_json::nlohmann_json)
# Bundled seed fallback for the registry resolver (env -> appdata -> this):
target_compile_definitions(chudware_materials PRIVATE
  CHUDWARE_BUNDLED_MATERIALS_REGISTRY="${CMAKE_CURRENT_SOURCE_DIR}/data/materials/registry.json")
target_compile_definitions(chudware_cam PRIVATE
  CHUDWARE_BUNDLED_MATERIALS_REGISTRY="${CMAKE_CURRENT_SOURCE_DIR}/data/materials/registry.json")

add_library(chudware_dfm STATIC
  dfm/process_profiles.cpp)
target_include_directories(chudware_dfm PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_dfm PUBLIC chudware_materials nlohmann_json::nlohmann_json)

add_executable(test_tolerance tests/test_tolerance.cpp)
target_include_directories(test_tolerance PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_tolerance PRIVATE chudware_tolerance)
target_compile_definitions(test_tolerance PRIVATE
  CHUDWARE_TEST_DATA_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/data")
set_target_properties(test_tolerance PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME tolerance COMMAND test_tolerance)

add_executable(test_mechanical tests/test_mechanical.cpp)
target_include_directories(test_mechanical PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_mechanical PRIVATE chudware_mechanical)
target_compile_definitions(test_mechanical PRIVATE
  CHUDWARE_TEST_DATA_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/data")
set_target_properties(test_mechanical PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME mechanical COMMAND test_mechanical)

add_executable(test_materials tests/test_materials.cpp)
target_include_directories(test_materials PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_materials PRIVATE chudware_materials)
target_compile_definitions(test_materials PRIVATE
  CHUDWARE_TEST_DATA_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/data"
  CHUDWARE_TEST_REGISTRY="${CMAKE_CURRENT_SOURCE_DIR}/data/materials/registry.json")
set_target_properties(test_materials PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME materials COMMAND test_materials)

add_executable(test_dfm tests/test_dfm.cpp)
target_include_directories(test_dfm PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_dfm PRIVATE chudware_dfm)
target_compile_definitions(test_dfm PRIVATE
  CHUDWARE_TEST_DATA_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/data"
  CHUDWARE_TEST_REGISTRY="${CMAKE_CURRENT_SOURCE_DIR}/data/materials/registry.json")
set_target_properties(test_dfm PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME dfm COMMAND test_dfm)

# --- libchudware piping + hydraulic (T-RF19 leaf 4) ------------------------------
# C++ ports of engine_oracle/piping/ (1-D liquid + gas network solver, fluid
# properties, B36 schedule catalog, CFD handoff math) and
# engine_oracle/hydraulic/ (ISO 1219 circuit schema, cylinder/pump/accumulator/
# valve sizing, BOM) plus the services/mcp tool envelopes for both families
# (incl. the aicad.simhandoff/1 builders they share). PURE (OCCT-free, no
# doc/ecad/agent linkage): the `fluids` algorithms the oracle calls (Clamond
# friction factor, Rennels fitting K) are re-implemented and the B36.10M/19M
# schedule tables + B16 fitting CSV are vendored (pipe_tables.h, generated
# from the MIT fluids package; fittings_data.h), so the port always behaves
# like a fluids-present box. Each family owns its methods behind a single
# call(method, params, result) dispatch entry for the libchudware glue.
add_library(chudware_piping STATIC
  piping/catalog.cpp
  piping/cfd.cpp
  piping/fluid_properties.cpp
  piping/piping.cpp
  piping/schema.cpp
  piping/simhandoff.cpp
  piping/solve.cpp)
target_include_directories(chudware_piping PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_piping PUBLIC nlohmann_json::nlohmann_json)
if(MSVC)
  # MSVC only defines M_PI et al. in <cmath> when _USE_MATH_DEFINES is set.
  target_compile_definitions(chudware_piping PUBLIC _USE_MATH_DEFINES)
endif()

add_library(chudware_hydraulic STATIC
  hydraulic/bom.cpp
  hydraulic/handoff.cpp
  hydraulic/hydraulic.cpp
  hydraulic/schema.cpp
  hydraulic/solve.cpp)
target_include_directories(chudware_hydraulic PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_hydraulic PUBLIC chudware_piping nlohmann_json::nlohmann_json)
if(MSVC)
  target_compile_definitions(chudware_hydraulic PUBLIC _USE_MATH_DEFINES)
endif()

add_executable(test_piping tests/test_piping.cpp)
target_include_directories(test_piping PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_piping PRIVATE chudware_piping)
set_target_properties(test_piping PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME piping COMMAND test_piping)

add_executable(test_hydraulic tests/test_hydraulic.cpp)
target_include_directories(test_hydraulic PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_hydraulic PRIVATE chudware_hydraulic)
set_target_properties(test_hydraulic PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME hydraulic COMMAND test_hydraulic)

# {"method","params"} per line on stdin -> dispatch result per line; the T-RF19
# parity harness drives this against the Python oracle (mirrors fw_cli).
add_executable(rpc_cli piping/rpc_cli.cpp)
target_link_libraries(rpc_cli PRIVATE chudware_hydraulic chudware_piping)
set_target_properties(rpc_cli PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# --- Wave 2: OCCT geometry kernel (opt-in) -----------------------------------
if(CHUDWARE_WITH_OCCT)
  include(ProvisionOCCT)

  # --- Heavy from-source third-party sub-deps (opt-in per platform) -----------
  # On Windows each of these auto-provisions (builds from source / fetches a wheel-
  # matched binary). Off-Windows they used to default hard OFF, which is wrong for a
  # tree that has ALREADY provisioned one: the artifacts sit in third_party/ and cost
  # nothing to link, so a present dependency now defaults ON (see _def_* below).
  # Nothing provisioned and not Windows -> OFF, so a fresh checkout never pays an
  # unasked from-source build; opt in with -DCHUDWARE_WITH_<DEP>=ON.
  # The dependent code is #ifdef-guarded either way (doc/formats.cpp for
  # LIB3MF/OPENNURBS, libchudware.cpp + the chudware_mesh target for NETGEN).

  # Provisioned-artifact probes — "is this dependency already built in-tree?"
  file(GLOB _have_netgen
       "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/netgen/install/lib/libnglib.so"
       "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/netgen/install/lib64/libnglib.so"
       "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/netgen/install/lib/nglib.lib")
  file(GLOB _have_lib3mf
       "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/lib3mf/*/lib/lib3mf.so"
       "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/lib3mf/*/lib/lib3mf.lib")
  file(GLOB _have_opennurbs
       "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/opennurbs/*/lib/*opennurbs*")

  # --- One-time migration of pre-rebrand caches (605645b7) --------------------
  # The Billet / AI-CAD -> Chudware rename renamed AICAD_WITH_* to CHUDWARE_WITH_*.
  # CMake does NOT rename cache entries: every existing build tree kept its ON value
  # under the now-DEAD AICAD_WITH_* name while the renamed option silently took its
  # off-Windows OFF default. Whole features were compiled out of libchudware with no
  # error and no warning — the FEA surface (sim_mesh / sim_run_static / sim_run_modal
  # / sim_run_fatigue / sim_run_thermal) simply became "unknown method" at runtime.
  #
  # Adopt the legacy value once, but only auto-enable when the dependency is actually
  # provisioned, so the migration can never turn a working configure into a surprise
  # third-party build. A legacy ON we cannot honor warns loudly instead of vanishing.
  # The dead entry is dropped either way, so this never re-fires.
  foreach(_f NETGEN LIB3MF OPENNURBS)
    if(DEFINED AICAD_WITH_${_f})
      string(TOLOWER "${_f}" _fl)
      set(_want "${AICAD_WITH_${_f}}")
      if(_want AND NOT WIN32 AND NOT _have_${_fl})
        message(WARNING
          "[chudware] pre-rebrand cache had AICAD_WITH_${_f}=ON, but nothing is "
          "provisioned under third_party/${_fl}. Leaving CHUDWARE_WITH_${_f} at its "
          "default; pass -DCHUDWARE_WITH_${_f}=ON to build it from source.")
      else()
        set(CHUDWARE_WITH_${_f} "${_want}" CACHE BOOL
            "migrated from AICAD_WITH_${_f}" FORCE)
        message(STATUS
          "[chudware] migrated AICAD_WITH_${_f}=${_want} -> CHUDWARE_WITH_${_f}")
      endif()
      unset(AICAD_WITH_${_f} CACHE)  # drop the dead pre-rebrand entry
    endif()
  endforeach()

  if(WIN32)
    set(_aicad_subdep_default ON)
  else()
    set(_aicad_subdep_default OFF)
  endif()
  set(_def_netgen    ${_aicad_subdep_default})
  set(_def_lib3mf    ${_aicad_subdep_default})
  set(_def_opennurbs ${_aicad_subdep_default})
  if(_have_netgen)
    set(_def_netgen ON)
  endif()
  if(_have_lib3mf)
    set(_def_lib3mf ON)
  endif()
  if(_have_opennurbs)
    set(_def_opennurbs ON)
  endif()
  option(CHUDWARE_WITH_NETGEN    "Netgen/nglib FEA tet mesher (sim/mesher.cpp, fea_run)" ${_def_netgen})
  option(CHUDWARE_WITH_LIB3MF    "lib3mf 3MF reader/writer (doc/formats.cpp)"            ${_def_lib3mf})
  option(CHUDWARE_WITH_OPENNURBS "openNURBS Rhino .3dm reader/writer (doc/formats.cpp)"  ${_def_opennurbs})

  # --- P50: Netgen / nglib tet mesher (LGPL-2.1 — commercial-safe) -----------
  # BUILD Netgen from source against OUR OCCT (no prebuilt SDK exists); see
  # ProvisionNetgen.cmake. nglib.h lives ONLY in mesher.cpp (mesher.h is backend-
  # free); chudware_mesh links nglib's import lib and nglib.dll + ngcore.dll are staged
  # next to libchudware.dll. This replaced gmsh (GPL-2+, no linking exception) — the #1
  # commercialization blocker. MUST come AFTER ProvisionOCCT (shares its OCCT + zlib).
  if(CHUDWARE_WITH_NETGEN)
    include(ProvisionNetgen)
    # ELF runtime-path: nglib lives in the Netgen install lib dir (out of the loader's default
    # search). Bake that dir into the BUILD_RPATH of every target defined below (libchudware + the
    # FEA-mesh tests) so they resolve libnglib.so; libnglib itself has $ORIGIN rpath so it then
    # finds its sibling libngcore.so. No-op on Windows (DLLs are co-located/staged instead).
    if(NOT WIN32)
      get_filename_component(_ng_libdir "${NETGEN_IMPORT_LIB}" DIRECTORY)
      list(APPEND CMAKE_BUILD_RPATH "$ORIGIN" "${_ng_libdir}")
    endif()
  endif()

  # --- P55r: lib3mf 3MF reader/writer (BSD — clean to link/distribute) -------
  # Provision the SAME lib3mf 2.5.0 the Python oracle (engine/doc/formats.py)
  # wraps via the `lib3mf` wheel, so the 3MF mesh extraction (vertex coords +
  # triangle indices) is bit-identical. The header-only CppDynamic binding lives
  # ONLY in doc/formats.cpp and dlopens lib3mf.dll at runtime (no import lib on
  # the link line); chudware_doc compiles with CHUDWARE_WITH_LIB3MF + the binding's
  # include dir, and lib3mf.dll is staged next to libchudware.dll below.
  if(CHUDWARE_WITH_LIB3MF)
    include(ProvisionLib3mf)
  endif()

  # --- P55s: openNURBS (Rhino .3dm read/write) — MIT-ish, built FROM SOURCE ---
  # Provision + build opennurbsStatic from the SAME rhino3dm v8.17.0 the Python
  # oracle (engine/doc/formats.py) wraps via the `rhino3dm 8.17.0` wheel, so the
  # .3dm mesh extraction (ON_Mesh m_V float32 + m_F quad/tri) is bit-identical.
  # opennurbs.h lives ONLY in doc/formats.cpp; chudware_doc compiles with
  # CHUDWARE_WITH_OPENNURBS + the include dir and links opennurbsStatic below.
  if(CHUDWARE_WITH_OPENNURBS)
    include(ProvisionOpenNURBS)
  endif()

  # P50b: sim/fea_run.cpp (the combined mesh+resolve+solve glue) joins the mesher
  # target — it is the only sim source that needs the mesher, and adding it here
  # keeps the mesher-backend confinement intact (fea_run.h itself is nglib-free).
  # chudware_mesh gains a PUBLIC dep on chudware_sim so fea_run.cpp can call the FEA solvers.
  # Gated on CHUDWARE_WITH_NETGEN: the mesher (mesher.cpp) is the only nglib consumer.
  if(CHUDWARE_WITH_NETGEN)
    add_library(chudware_mesh STATIC sim/mesher.cpp sim/hex_mesher.cpp sim/fea_run.cpp sim/fea_fatigue.cpp)
    target_include_directories(chudware_mesh PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
    target_include_directories(chudware_mesh PRIVATE ${NETGEN_INCLUDE_DIRS})
    # nglib's import lib is PUBLIC so it propagates onto the final link line of the
    # `chudware` DLL that consumes this static lib (a STATIC lib's link deps aren't pulled
    # in transitively otherwise). nglib.h decorates its prototypes dllimport on WIN32.
    target_link_libraries(chudware_mesh PUBLIC ${NETGEN_IMPORT_LIB} chudware_sim nlohmann_json::nlohmann_json)
    set_property(TARGET chudware_mesh PROPERTY
      MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")
  endif()
  # Smoke test: box -> volume (BRepGProp) -> tessellation (BRepMesh). Proves the
  # auto-provisioned kernel is found, linked, and runnable end to end.
  add_executable(occt_smoke geom/occt_smoke.cpp)
  target_include_directories(occt_smoke PRIVATE ${OpenCASCADE_INCLUDE_DIR})
  # Minimal toolkit set for box + volume (BRepGProp) + mesh (BRepMesh) — avoids
  # forcing the Tcl/VTK/visualization DLLs to load at startup.
  target_link_libraries(occt_smoke PRIVATE
    TKernel TKMath TKG2d TKG3d TKGeomBase TKGeomAlgo TKBRep TKTopAlgo TKPrim TKMesh)
  set_target_properties(occt_smoke PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME occt_smoke COMMAND occt_smoke)
  # Prepend the OCCT toolkit + 3rdparty DLL dirs to PATH for the test run, via
  # ENVIRONMENT_MODIFICATION path_list_prepend (handles the path separator
  # correctly — a plain ENVIRONMENT "PATH=a;b" would be mangled by CMake's list ;).
  set(_occt_envmod "PATH=path_list_prepend:${OCCT_DLL_DIR}")
  foreach(_b ${OCCT_3RDPARTY_BINS})
    list(APPEND _occt_envmod "PATH=path_list_prepend:${_b}")
  endforeach()
  set_tests_properties(occt_smoke PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Wave 2 geom kernel: OCCT tessellation -> AICM emitter -----------------
  # tessellate.cpp + features.cpp + profile.cpp (OCCT) on top of the OCCT-free
  # aicm.cpp writer. profile.cpp compiles solved sketches (nlohmann::json) into
  # planar faces (lifted via chudware_geom's SketchPlane), so this lib now also links
  # chudware_geom (SketchPlane/Eigen) and nlohmann_json.
  add_library(chudware_geom_occt STATIC
    geom/tessellate.cpp
    geom/features.cpp
    geom/profile.cpp
    geom/profile3d.cpp
    geom/booleans.cpp
    geom/dress.cpp
    geom/transform.cpp
    geom/hole.cpp
    geom/sheetmetal.cpp
    geom/weldment.cpp
    geom/surfacing.cpp
    geom/curves.cpp
    geom/thread.cpp
    geom/text.cpp
    geom/ecad_3d.cpp
    doc/drawing.cpp
    sim/overlay.cpp
    geom/aicm.cpp)
  target_include_directories(chudware_geom_occt PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  # Modelling toolkits for box/prism (TKPrim), revolve (TKPrim), polygon->face->wire
  # (TKBRep/TKTopAlgo), adaptors + GC_MakeArcOfCircle (TKGeomBase/TKGeomAlgo), mesh
  # (TKMesh), healing (TKShHealing). booleans.cpp adds Fuse/Cut/Common (TKBO) and
  # dress.cpp adds MakeFillet/MakeChamfer (TKFillet). chudware_geom brings SketchPlane +
  # Eigen3::Eigen; nlohmann_json for the solved-entity dicts profile.cpp consumes.
  target_link_libraries(chudware_geom_occt PUBLIC
    chudware_geom
    chudware_prof   # per-phase sim timers (sim/overlay.cpp overlay_* studies)
    chudware_util   # aicm_handoff publish for sim/overlay AICM (T-PERF10)
    chudware_ecad   # ecad/schema.h pad_world + ecad/ops.h OpError for geom/ecad_3d.cpp
    nlohmann_json::nlohmann_json
    TKernel TKMath TKG2d TKG3d TKGeomBase TKGeomAlgo
    TKBRep TKTopAlgo TKPrim TKMesh TKShHealing TKBO TKFillet
    # TKHLR: hidden-line removal (HLRBRep_Algo / HLRBRep_HLRToShape) for the 2D
    # drawing projection (doc/drawing.cpp; engine/doc/drawing.py oracle).
    TKHLR
    # TKOffset: shell (BRepOffsetAPI_MakeThickSolid) + draft (BRepOffsetAPI_DraftAngle)
    # + loft (BRepOffsetAPI_ThruSections).
    TKOffset
    # TKV3d: StdPrs_BRepFont + StdPrs_BRepTextBuilder (FreeType glyph -> B-rep faces) for
    # the `text` emboss/engrave feature (geom/text.cpp). Pulls the FreeType-backed Font
    # package; its DLL is already staged by chudware_occt_copy_dlls.
    TKV3d
    # Data-exchange (doc/formats.cpp): the NON-CAF control classes — STEPControl_*
    # (TKDESTEP), IGESControl_* (TKDEIGES), StlAPI_Writer + RWStl (TKDESTL) over the XS
    # base (TKXSBase).
    TKDESTEP TKDEIGES TKDESTL TKXSBase
    # Mesh lane (P27): OCCT 7.9.3 splits the mesh readers/writers into per-format DE
    # toolkits — RWObj is in TKDEOBJ, RWGltf in TKDEGLTF, RWStl in TKDESTL (above), all
    # over the RWMesh base in TKRWMesh. The glTF reader/writer are XCAF-document-based
    # (RWGltf_CafReader/Writer), so we also bring the OCAF/XCAF stack (TKXCAF/TKLCAF/
    # TKCDF + the Bin persistence drivers TKBin/TKBinL/TKBinXCAF that XCAFApp_Application
    # ::NewDocument("BinXCAF") needs). OBJ/STL import use the non-CAF Poly_Triangulation
    # API (RWObj::ReadFile / RWStl::ReadFile); OBJ/PLY export use hand writers. All these
    # toolkit DLLs are already staged by chudware_occt_copy_dlls (copy_directory of the
    # whole OCCT bin dir).
    TKRWMesh TKDEOBJ TKDEGLTF TKXCAF TKLCAF TKCDF TKBin TKBinL TKBinXCAF)

  # --- Netgen-mesher-dependent wiring + tests (gated on CHUDWARE_WITH_NETGEN) ----
  if(CHUDWARE_WITH_NETGEN)
  # sim/fea_fatigue.cpp (in chudware_mesh, defined above) calls fatigue_overlay from
  # sim/overlay.cpp (in chudware_geom_occt). Wire that dep here — AFTER chudware_geom_occt
  # exists — so the fatigue solver can package its display overlay. PRIVATE: only
  # fea_fatigue.cpp's TU needs it; consumers of chudware_mesh link chudware_geom_occt on
  # their own (the final chudware DLL already does).
  target_link_libraries(chudware_mesh PRIVATE chudware_geom_occt)

  # Fatigue solver smoke test: inline 6-tet cube under tension -> fea_run_fatigue.
  # No STEP path -> no overlay, so it needs no OCCT DLL PATH munging, but it still
  # links the OCCT-gated targets (this whole block is CHUDWARE_WITH_OCCT-only).
  add_executable(test_fatigue tests/test_fatigue.cpp)
  target_include_directories(test_fatigue PRIVATE ${doctest_SOURCE_DIR})
  target_link_libraries(test_fatigue PRIVATE chudware_mesh chudware_geom_occt)
  set_target_properties(test_fatigue PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME fatigue COMMAND test_fatigue)
  # Netgen + OCCT DLLs must be on PATH for the static solve (fea_run_static meshes only
  # when a STEP path is present — the inline-mesh test does not — but chudware_mesh pulls
  # the nglib import lib, and chudware_geom_occt pulls OCCT, so stage both dirs to be safe).
  set(_fatigue_envmod "PATH=path_list_prepend:${OCCT_DLL_DIR}")
  foreach(_b ${OCCT_3RDPARTY_BINS})
    list(APPEND _fatigue_envmod "PATH=path_list_prepend:${_b}")
  endforeach()
  list(APPEND _fatigue_envmod "PATH=path_list_prepend:${NETGEN_ROOT}/install/bin")
  get_filename_component(_ng_zlib_bin "${NETGEN_ZLIB_DLL}" DIRECTORY)
  list(APPEND _fatigue_envmod "PATH=path_list_prepend:${_ng_zlib_bin}")
  set_tests_properties(fatigue PROPERTIES ENVIRONMENT_MODIFICATION "${_fatigue_envmod}")

  # Netgen mesher end-to-end: build a cantilever OCCT solid -> STEP -> mesh_step (tet4/tet10)
  # -> fea_run_static, asserting quality + convergence toward the Euler-Bernoulli deflection
  # (Bug F7 guard). Exercises the REAL nglib mesh path, so it needs OCCT + Netgen DLLs (the
  # same _fatigue_envmod PATH prepend).
  add_executable(test_netgen_mesh tests/test_netgen_mesh.cpp)
  target_include_directories(test_netgen_mesh PRIVATE
    ${doctest_SOURCE_DIR} ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_netgen_mesh PRIVATE chudware_mesh chudware_geom_occt)
  set_target_properties(test_netgen_mesh PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME netgen_mesh COMMAND test_netgen_mesh)
  set_tests_properties(netgen_mesh PROPERTIES ENVIRONMENT_MODIFICATION "${_fatigue_envmod}")

  # fea_profile: standalone profiling harness for the static-stress solve (NOT a ctest — a
  # manual perf tool). Meshes a beam -> fea_run_static under the in-process sampling profiler
  # + CHUDWARE_FEA_PROFILE phase timers, sweeping fineness. Same OCCT/Netgen DLL deps as above.
  add_executable(fea_profile tools/fea_profile.cpp)
  target_include_directories(fea_profile PRIVATE ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(fea_profile PRIVATE chudware_mesh chudware_geom_occt chudware_prof)
  set_target_properties(fea_profile PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

  # Mesher auto-pick + exotic-type regression: structured-hex fallback to tet on non-box,
  # auto_type hex/tet selection, thin-wall layering, quad_surface, detect_box_geometry.
  add_executable(test_mesher_autopick tests/test_mesher_autopick.cpp)
  target_include_directories(test_mesher_autopick PRIVATE
    ${doctest_SOURCE_DIR} ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_mesher_autopick PRIVATE chudware_mesh chudware_geom_occt)
  set_target_properties(test_mesher_autopick PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME mesher_autopick COMMAND test_mesher_autopick)
  set_tests_properties(mesher_autopick PROPERTIES ENVIRONMENT_MODIFICATION "${_fatigue_envmod}")

  # Mesh-quality (evenness) regression: cylinder -> STEP -> mesh_step, assert an EVEN tet mesh
  # (bounded aspect_max, jacobian_min>0, low pct_poor, healthy curvature resolution).
  add_executable(test_mesh_quality tests/test_mesh_quality.cpp)
  target_include_directories(test_mesh_quality PRIVATE
    ${doctest_SOURCE_DIR} ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_mesh_quality PRIVATE chudware_mesh chudware_geom_occt)
  set_target_properties(test_mesh_quality PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME mesh_quality COMMAND test_mesh_quality)
  set_tests_properties(mesh_quality PROPERTIES ENVIRONMENT_MODIFICATION "${_fatigue_envmod}")

  # Advanced multi-feature parts (boss+pocket+chamfer, hole array, stepped fillet) built via
  # OCCT B-rep -> STEP -> mesh_step (tet10) + fea_run_static: asserts mesh-quality invariants
  # (no inverted/degenerate elements, positive volumes), a finite plausible static solve
  # (nonzero disp+stress, finite FoS), and a generous per-part wall-clock ceiling ("optimized
  # enough to run"). Same OCCT + Netgen DLL PATH prepend as the other mesher tests.
  add_executable(test_mesher_advanced tests/test_mesher_advanced.cpp)
  target_include_directories(test_mesher_advanced PRIVATE
    ${doctest_SOURCE_DIR} ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_mesher_advanced PRIVATE chudware_mesh chudware_geom_occt)
  set_target_properties(test_mesher_advanced PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME mesher_advanced COMMAND test_mesher_advanced)
  # RUN_SERIAL: mesher_advanced takes ~56 s standalone and FAILS under `ctest -j6`
  # contention (REFACTOR_LOG.md:7003-7004) — the Netgen legs are CPU-bound and lose
  # their budget to the parallel load, exactly like the `firmware` suite above. The
  # timeout stays at ctest's default; serialization alone is what makes it pass.
  set_tests_properties(mesher_advanced PROPERTIES
    RUN_SERIAL TRUE
    ENVIRONMENT_MODIFICATION "${_fatigue_envmod}")

  # Durable sim solve-state cache: FEA static HIT/MISS + .step+ studies/*.npz round-trip
  # + SPICE/CFD result cache invalidation. Needs the real mesh path (chudware_mesh) + stepplus
  # (chudware_doc) so it links the OCCT+Netgen targets like test_fatigue (same _fatigue_envmod).
  add_executable(test_sim_cache tests/test_sim_cache.cpp)
  target_include_directories(test_sim_cache PRIVATE ${doctest_SOURCE_DIR} ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_sim_cache PRIVATE chudware_mesh chudware_doc chudware_geom_occt chudware_sim chudware_sketch chudware_geom chudware_util)
  set_target_properties(test_sim_cache PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME sim_cache COMMAND test_sim_cache)
  set_tests_properties(sim_cache PROPERTIES ENVIRONMENT_MODIFICATION "${_fatigue_envmod}")
  endif()  # CHUDWARE_WITH_NETGEN

  # FEA boundary-surface extractor: fea_surface_body (the result-plot display surface,
  # so a bent beam renders curved). No STEP/mesher — builds structured tet meshes in
  # code — but fea_surface_body lives in chudware_geom_occt, so it links + needs OCCT DLLs.
  add_executable(test_overlay_surface tests/test_overlay_surface.cpp)
  target_include_directories(test_overlay_surface PRIVATE
    ${doctest_SOURCE_DIR} ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_overlay_surface PRIVATE chudware_geom_occt)
  set_target_properties(test_overlay_surface PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME overlay_surface COMMAND test_overlay_surface)
  set_tests_properties(overlay_surface PROPERTIES
    ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # T-SIM2: regenerator for the committed result-contour AICM fixture used by the
  # CHUDWARE_SHOT=fea_overlay headless scenario. Builds a structured cantilever tet mesh in code
  # (Netgen-free — the mesher is the only unavailable piece in CI), runs the real fea_static_solve,
  # and skins + field-maps it via static_overlay (in chudware_geom_occt). Not a ctest (it writes a
  # file); run by hand to refresh contracts/fixtures/sim2_cantilever_result.aicm.
  add_executable(gen_sim2_result_fixture tests/gen_sim2_result_fixture.cpp)
  target_include_directories(gen_sim2_result_fixture PRIVATE ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(gen_sim2_result_fixture PRIVATE chudware_geom_occt chudware_sim)
  set_target_properties(gen_sim2_result_fixture PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

  # T-BL27: surf_replace_face_solid heal regression (cut-through + coincident-patch cases).
  # Own file/target (test_surfacing / core/tests/test_surfacing.cpp is T-BL28's ruled/fill/
  # boundary body-edge-selector suite — landed independently, same insertion point).
  add_executable(test_surf_replace_face_heal tests/test_surf_replace_face_heal.cpp)
  target_include_directories(test_surf_replace_face_heal PRIVATE
    ${doctest_SOURCE_DIR} ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_surf_replace_face_heal PRIVATE chudware_geom_occt)
  set_target_properties(test_surf_replace_face_heal PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME surf_replace_face_heal COMMAND test_surf_replace_face_heal)
  set_tests_properties(surf_replace_face_heal PROPERTIES
    ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # Verification: tessellate -> write_aicm -> HOST render::parseAicm round-trip.
  add_executable(test_geom_aicm
    tests/test_geom_aicm.cpp)
  # host/src/render (the loader's dir) — NOT host/src, whose math.h shadows the
  # CRT <math.h> and breaks <cmath>/<cstdlib> under MSVC.
  target_include_directories(test_geom_aicm PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render)
  target_link_libraries(test_geom_aicm PRIVATE chudware_geom_occt chudware_util)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_geom_aicm PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render/aicm_loader.cpp)
  set_target_properties(test_geom_aicm PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME geom_aicm COMMAND test_geom_aicm)
  # Same OCCT DLL PATH prepend as occt_smoke so the test can load TKernel et al.
  set_tests_properties(geom_aicm PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Wave 2 milestone: solved sketch -> profile -> extrude/revolve ---------
  # End-to-end: chudware::sketch::solve_sketch -> faces_from_solved_sketch ->
  # extrude_face/revolve_face -> tessellate -> write_aicm -> HOST render::parseAicm.
  # Links chudware_sketch for solve_sketch (chudware_geom_occt already brings the geom
  # kernel + chudware_geom + nlohmann_json transitively).
  add_executable(test_profile_extrude
    tests/test_profile_extrude.cpp)
  target_include_directories(test_profile_extrude PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render)
  target_link_libraries(test_profile_extrude PRIVATE
    chudware_geom_occt chudware_sketch chudware_geom chudware_util)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_profile_extrude PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render/aicm_loader.cpp)
  set_target_properties(test_profile_extrude PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME profile_extrude COMMAND test_profile_extrude)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(profile_extrude PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Wave 2 milestone: boolean + dress-up ops compose into real parts ------
  # End-to-end: make_box/extrude_face base -> fuse/cut/common (booleans.h) +
  # fillet/chamfer (dress.h) -> tessellate -> write_aicm -> HOST render::parseAicm.
  # Same wiring as test_profile_extrude (chudware_geom_occt now carries TKBO/TKFillet).
  add_executable(test_ops
    tests/test_ops.cpp)
  target_include_directories(test_ops PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render)
  target_link_libraries(test_ops PRIVATE
    chudware_geom_occt chudware_sketch chudware_geom chudware_util)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_ops PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render/aicm_loader.cpp)
  set_target_properties(test_ops PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME ops COMMAND test_ops)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(ops PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Sweep robustness: geom::sweep_profile (MakePipeShell) + helix paths ----
  # Pure-geom analytic suite: cylinder / hollow tube / 3-turn helix coil / L-elbow
  # swept-volume checks proving the kernel caps into valid solids on curved spines.
  add_executable(test_sweep
    tests/test_sweep.cpp)
  target_include_directories(test_sweep PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_sweep PRIVATE
    chudware_geom_occt chudware_geom)
  set_target_properties(test_sweep PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME sweep COMMAND test_sweep)
  set_tests_properties(sweep PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Sketch arc + path-wire: profile.cpp edge/wire builder (round-3 N-2 / N-3) -----
  # arc `ccw` honored + degrees, and a mixed line+arc path stitches across a float gap.
  add_executable(test_profile_arc
    tests/test_profile_arc.cpp)
  target_include_directories(test_profile_arc PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_profile_arc PRIVATE
    chudware_geom_occt chudware_geom)
  set_target_properties(test_profile_arc PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME profile_arc COMMAND test_profile_arc)
  set_tests_properties(profile_arc PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Import / save round-trip: N-8 bake, N-5 multi-body, N-4 + manual-open routing --
  add_executable(test_import_roundtrip
    tests/test_import_roundtrip.cpp)
  target_include_directories(test_import_roundtrip PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render)
  target_link_libraries(test_import_roundtrip PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom chudware_util)
  target_sources(test_import_roundtrip PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render/aicm_loader.cpp)
  set_target_properties(test_import_roundtrip PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME import_roundtrip COMMAND test_import_roundtrip)
  set_tests_properties(import_roundtrip PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Assembly export: doc_export_file/step/estimate must world-place component bodies --
  # (regression for "file exports do nothing for assemblies" — exports compiled the
  # assembly doc as a part, which has no features, so the writer got zero bodies).
  add_executable(test_export_assembly
    tests/test_export_assembly.cpp)
  target_include_directories(test_export_assembly PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_export_assembly PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_export_assembly PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME export_assembly COMMAND test_export_assembly)
  set_tests_properties(export_assembly PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Assembly insert: asm_insert_component places components into the tree ---
  # Locks the host Insert Component contract (new assembly → insert source path →
  # component appears, first is fixed, second is floating).
  add_executable(test_insert_component
    tests/test_insert_component.cpp)
  target_include_directories(test_insert_component PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_insert_component PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_insert_component PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME insert_component COMMAND test_insert_component)
  set_tests_properties(insert_component PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- simulate_motion: assembly kinematic sweep smoke test -------------------
  # Exercises doc_simulate_motion end-to-end: assembly + angle mate + sweep 0->90 deg.
  # Linked like test_export_assembly (needs STEP writer via chudware_geom_occt).
  add_executable(test_simulate_motion
    tests/test_simulate_motion.cpp)
  target_include_directories(test_simulate_motion PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_simulate_motion PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_simulate_motion PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME simulate_motion COMMAND test_simulate_motion)
  set_tests_properties(simulate_motion PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Wave 2 milestone: the document model — a rebuildable feature tree ------
  # core/doc/document.cpp orchestrates solve_sketch -> faces_from_solved_sketch ->
  # extrude/revolve -> fuse/cut/common -> fillet/chamfer into a part, keyed by
  # feature id, and RE-RUNS on a parameter edit (the parametric payoff). It links
  # chudware_geom_occt (the OCCT kernel + chudware_geom + nlohmann_json, transitively),
  # chudware_sketch (solve_sketch), and chudware_util (the expr evaluator for expr dims).
  add_library(chudware_doc STATIC
    doc/document.cpp
    doc/selectors.cpp
    doc/picks.cpp
    doc/session.cpp
    doc/motion_dynamics.cpp
    doc/recon.cpp
    doc/assembly.cpp
    doc/sketch_geom.cpp
    doc/stepplus.cpp
    doc/validate.cpp
    doc/op_registry.cpp
    doc/formats.cpp
    doc/dxf.cpp
    doc/pdfvec.cpp
    doc/annot_draw.cpp
    doc/export2d.cpp
    doc/drawing_session.cpp
    cam/extract.cpp)
  target_include_directories(chudware_doc PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  # 3MF (doc/formats.cpp): the lib3mf CppDynamic binding header + the enabling
  # define, PRIVATE so the binding stays confined to formats.cpp's TU and does
  # not leak onto chudware_doc's public include interface. (gated: CHUDWARE_WITH_LIB3MF —
  # formats.cpp compiles out its 3MF path when the define is absent.)
  if(CHUDWARE_WITH_LIB3MF)
    target_include_directories(chudware_doc PRIVATE ${LIB3MF_INCLUDE_DIR})
    target_compile_definitions(chudware_doc PRIVATE CHUDWARE_WITH_LIB3MF)
  endif()
  # openNURBS (.3dm): the opennurbs include dir + the enabling define, PRIVATE so
  # opennurbs.h stays confined to formats.cpp's TU; opennurbsStatic is linked
  # PUBLIC so it propagates onto the final libchudware DLL link line. UNICODE/_UNICODE
  # are required by opennurbs.h (its ON_wString APIs assume the wide CRT) — set
  # PRIVATE on chudware_doc so only this lib's TUs see them. (gated: CHUDWARE_WITH_OPENNURBS —
  # formats.cpp compiles out its .3dm path when the define is absent.)
  if(CHUDWARE_WITH_OPENNURBS)
    target_include_directories(chudware_doc PRIVATE ${OPENNURBS_INCLUDE_DIR})
    target_compile_definitions(chudware_doc PRIVATE CHUDWARE_WITH_OPENNURBS UNICODE _UNICODE)
  endif()
  target_link_libraries(chudware_doc PUBLIC
    chudware_geom_occt chudware_sketch chudware_geom chudware_util chudware_sim
    chudware_mate
    chudware_miniz
    chudware_parts
    nlohmann_json::nlohmann_json)
  if(CHUDWARE_WITH_OPENNURBS)
    if(WIN32)
      target_link_libraries(chudware_doc PUBLIC opennurbsStatic)
    else()
      # opennurbsStatic (MY_ZCALLOC -> it provides zcalloc/zcfree) and its bundled `zlib`
      # archive (which CALLS zcfree but, built with MY_ZCALLOC, doesn't define it) form a
      # circular static-lib dependency. MSVC's linker rescans automatically; GNU ld needs
      # both archives inside one --start-group/--end-group, which $<LINK_GROUP:RESCAN> emits.
      target_link_libraries(chudware_doc PUBLIC "$<LINK_GROUP:RESCAN,opennurbsStatic,zlib>")
    endif()
  endif()

  # Verification: compile a document -> write_aicm -> HOST render::parseAicm, with
  # the parametric-rebuild case (same doc, two envs -> two solids). Same wiring as
  # test_ops; chudware_doc carries the OCCT toolkits transitively via chudware_geom_occt.
  add_executable(test_document
    tests/test_document.cpp)
  target_include_directories(test_document PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render)
  target_link_libraries(test_document PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom chudware_util)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_document PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render/aicm_loader.cpp)
  set_target_properties(test_document PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME document COMMAND test_document)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(document PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Op vocabulary registry lockstep (mega-8 OpVocab) -----------------------
  # Fails if someone adds an apply_one handler without updating op_registry, or
  # if unknown-op errors lose their closest-suggestion text.
  add_executable(test_op_registry
    tests/test_op_registry.cpp)
  target_include_directories(test_op_registry PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_op_registry PRIVATE
    chudware_doc chudware_ecad chudware_geom_occt chudware_sketch chudware_geom chudware_util)
  set_target_properties(test_op_registry PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME op_registry COMMAND test_op_registry)
  set_tests_properties(op_registry PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Format-registry parity (T-RF19): doc_formats payload vs the Python oracle
  # dump (engine/doc/formats.py::list_formats) + detect_format lockstep. --------
  add_executable(test_formats_registry
    tests/test_formats_registry.cpp)
  target_include_directories(test_formats_registry PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_formats_registry PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom chudware_util)
  set_target_properties(test_formats_registry PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME formats_registry COMMAND test_formats_registry)
  set_tests_properties(formats_registry PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- sketch3d document layer: wire chaining, sweep spine/guide from a 3D
  # free-space sketch, and the sketch3d_health/drag session RPCs. --------------
  add_executable(test_sketch3d_doc
    tests/test_sketch3d_doc.cpp)
  target_include_directories(test_sketch3d_doc PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_sketch3d_doc PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_sketch3d_doc PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME sketch3d_doc COMMAND test_sketch3d_doc)
  set_tests_properties(sketch3d_doc PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Sketch-on-face DISPLAY fix (doc/session.cpp resolve_sketch_plane) -------
  # A sketch whose refs.plane is a kind=="face" selector must ENTER on that face:
  # doc_sketch_enter's plane_frame origin at the face offset, not SketchPlane::top()
  # (origin 0,0,0 — the reported display bug). Builds a box (rect sketch + extrude H)
  # via doc_apply_ops, adds a second sketch on a face selector targeting the top face,
  # and asserts the entered plane sits at z=H.
  add_executable(test_sketch_on_face
    tests/test_sketch_on_face.cpp)
  target_include_directories(test_sketch_on_face PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_sketch_on_face PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_sketch_on_face PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME sketch_on_face COMMAND test_sketch_on_face)
  set_tests_properties(sketch_on_face PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Circle rim drag-resize through the doc_sketch_drag RPC path -------------
  # The host drags a circle/arc rim via doc_sketch_enter -> doc_sketch_drag(".rim") ->
  # doc_sketch_drag_commit. Proves the rim handle resizes the radius AND that a failed
  # drag still returns entities (so the overlay is never wiped).
  add_executable(test_sketch_drag_rpc
    tests/test_sketch_drag_rpc.cpp)
  target_include_directories(test_sketch_drag_rpc PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_sketch_drag_rpc PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_sketch_drag_rpc PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME sketch_drag_rpc COMMAND test_sketch_drag_rpc)
  set_tests_properties(sketch_drag_rpc PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- T-W2 sketch completion suite (auto-coincident + sketch_health) ----------
  # Auto-coincident closes touching-corner profiles (default on, rollback-safe on
  # conflict) and doc_sketch_health narrates closure / DOF free variables / advisory.
  # RPC-level (doc_new -> doc_apply_ops -> doc_sketch_auto_constrain / _health), headless.
  add_executable(test_sketch_autoconstrain
    tests/test_sketch_autoconstrain.cpp)
  target_include_directories(test_sketch_autoconstrain PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_sketch_autoconstrain PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_sketch_autoconstrain PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME sketch_autoconstrain COMMAND test_sketch_autoconstrain)
  set_tests_properties(sketch_autoconstrain PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")
  # --- T-BL43 Convert Entities / external sketch references --------------------
  # Kernel: project_edges_to_plane (recon.cpp) — orthographic GeomProjLib projection then
  # classify (oblique circle -> ellipse, in-plane circle -> circle, perpendicular edge ->
  # point). Op: sketch_convert (session.cpp) — the op both GUIs emit; was contracted in
  # doc_ops.md C53 but unimplemented (every Convert Entities click failed 'unsupported').
  add_executable(test_sketch_convert
    tests/test_sketch_convert.cpp)
  target_include_directories(test_sketch_convert PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_sketch_convert PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_sketch_convert PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME sketch_convert COMMAND test_sketch_convert)
  set_tests_properties(sketch_convert PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Sketch power-trim: over-trim + DOF-preservation regression -------------
  # Guards the "swasi_test" report: construction geometry must act as a trim boundary
  # (no over-trim past it) and trimming a free tip must re-anchor constraints onto the
  # surviving spans (preserve DOF) instead of dropping every ref to the entity.
  add_executable(test_sketch_trim
    tests/test_sketch_trim.cpp)
  target_include_directories(test_sketch_trim PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_sketch_trim PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_sketch_trim PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME sketch_trim COMMAND test_sketch_trim)
  set_tests_properties(sketch_trim PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Simulation-study persistence + mutate RPCs (doc_sim_study_*) -----------
  # Exercises the full study lifecycle (add/edit/list/save+open/delete) on a blank
  # part. doc_new/doc_open compile the empty document through OCCT, so this is gated
  # under CHUDWARE_WITH_OCCT and links chudware_doc (which carries the OCCT toolkits via
  # chudware_geom_occt). No AICM loader needed — it asserts on the studies JSON directly.
  add_executable(test_studies
    tests/test_studies.cpp)
  target_include_directories(test_studies PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_studies PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_studies PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME studies COMMAND test_studies)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(studies PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- On-request labelled views (task 03: doc_render_view / contact sheet) -----
  # Builds an asymmetric box session doc and renders named / parametric-angle / explicit-dir views
  # plus a multi-view contact sheet, asserting distinct cameras yield distinct PNGs and equivalent
  # specs are byte-identical. Links chudware_doc (OCCT toolkits via chudware_geom_occt).
  add_executable(test_render_view
    tests/test_render_view.cpp)
  target_include_directories(test_render_view PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_render_view PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_render_view PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME render_view COMMAND test_render_view)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(render_view PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Exact perspective camera (doc_render_view.camera_pose.v1) -----------------
  # Drives the C ABI: a hand-projected cube silhouette, the apparent-scale ratio only a
  # perspective camera can produce, frustum clipping, every named degeneracy refusal, and
  # pinned pre-change hashes proving the legacy orthographic specs stayed byte-identical.
  add_executable(test_render_camera
    tests/test_render_camera.cpp)
  target_include_directories(test_render_camera PRIVATE
    ${doctest_SOURCE_DIR})
  target_link_libraries(test_render_camera PRIVATE
    chudware chudware_miniz nlohmann_json::nlohmann_json)
  set_target_properties(test_render_camera PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME render_camera COMMAND test_render_camera)
  set_tests_properties(render_camera PROPERTIES
    ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Per-body appearance (task 04: set_appearance op + AICM colour/material bake) ---
  # Ports the set_appearance op into the C++ core and bakes doc["appearances"] onto tessellated
  # bodies; the test drives the op end to end (doc field + both tess paths + .step+ round-trip).
  add_executable(test_appearance
    tests/test_appearance.cpp)
  target_include_directories(test_appearance PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_appearance PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_appearance PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME appearance COMMAND test_appearance)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(appearance PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Regular-polygon convenience entity (named ngons + generic sides:N) -------
  # Drives sketch_add_entity with named polygons (triangle..dodecagon) and {type:"polygon",sides:N}
  # through doc_apply_ops, asserting each expands to the right count of closed line segments. Links
  # chudware_doc (OCCT toolkits via chudware_geom_occt) since doc_new/doc_apply_ops compile the document.
  add_executable(test_polygon
    tests/test_polygon.cpp)
  target_include_directories(test_polygon PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_polygon PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_polygon PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME polygon COMMAND test_polygon)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(polygon PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Rectangle convenience entity (center+width/height and two-corner forms) ---
  # Drives sketch_add_entity with {type:"rectangle",...} through doc_apply_ops, asserting
  # it expands to 4 closed CCW lines, extrudes to the right area, and rejects malformed
  # input with a clear OpError. Mirrors test_polygon.
  add_executable(test_rectangle
    tests/test_rectangle.cpp)
  target_include_directories(test_rectangle PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_rectangle PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_rectangle PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME rectangle COMMAND test_rectangle)
  set_tests_properties(rectangle PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- GD&T + custom-section drawing coverage (annot_draw + section modes) ------
  # Builds a real holed block, makes a drawing, adds every GD&T element + a half
  # section, and asserts the shared annotation compiler emits primitives, the section
  # produces faces + hatch, and the DXF export pools the GD&T strokes. chudware_doc carries
  # the OCCT toolkits via chudware_geom_occt.
  add_executable(test_drawing_gdt
    tests/test_drawing_gdt.cpp)
  target_include_directories(test_drawing_gdt PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_drawing_gdt PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_drawing_gdt PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME drawing_gdt COMMAND test_drawing_gdt)
  set_tests_properties(drawing_gdt PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- A3: pattern feature compilers — linear_pattern / circular_pattern ------
  # core/doc/document.cpp's pattern branches REPLICATE a referenced seed solid with
  # rigid transforms (geom/transform.cpp: translate_copy / rotate_copy) and FUSE the
  # copies into one body (the geometry-pattern strategy of engine/doc/features/pattern.py,
  # in the simplified id-ref model). The test round-trips a linear + a circular pattern
  # document through the host AICM loader (the contract oracle) and asserts analytic,
  # OCCT-version-independent invariants (face counts, bbox, mesh volume). Same wiring as
  # test_document; chudware_doc carries the OCCT toolkits transitively via chudware_geom_occt.
  add_executable(test_pattern
    tests/test_pattern.cpp)
  target_include_directories(test_pattern PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render)
  target_link_libraries(test_pattern PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom chudware_util)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_pattern PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render/aicm_loader.cpp)
  set_target_properties(test_pattern PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME pattern COMMAND test_pattern)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(pattern PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- A4: mirror feature compiler — reflect a body about a plane ------------
  # core/doc/document.cpp's mirror branch reflects a referenced seed solid across a
  # plane (a FACE SELECTOR or an explicit plane_point/normal) via a mirror gp_Trsf
  # (geom/transform.cpp: mirror_copy) and either FUSES the reflection into the body
  # (merge:true, default) or keeps it as a separate body (merge:false). The test
  # round-trips an explicit-plane merge, a face-selector merge, and a no-merge
  # document through the host AICM loader (the contract oracle) and asserts analytic,
  # OCCT-version-independent invariants (face counts, bbox, mesh volume). Same wiring
  # as test_pattern; chudware_doc carries the OCCT toolkits transitively via chudware_geom_occt.
  add_executable(test_mirror
    tests/test_mirror.cpp)
  target_include_directories(test_mirror PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render)
  target_link_libraries(test_mirror PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom chudware_util)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_mirror PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render/aicm_loader.cpp)
  set_target_properties(test_mirror PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME mirror COMMAND test_mirror)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(mirror PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- B5: ctest coverage for eval_interference's B4 params (include_coincident /
  # min_clearance_mm), doc_eval_feature_overlap (B2 intra-part tool-solid overlap: boss_
  # swallowed/cut_overlap/dead_features), and doc_eval_compare (C1 shape-vs-shape match_pct
  # + residual_regions). Calls doc_new/doc_apply_ops/doc_eval_* directly on primitive box/
  # cylinder features and asserts on the returned JSON -- no host AICM loader needed (no
  # mesh parsing here, unlike test_document/test_mirror/test_hole), so this target skips
  # the render include path and the aicm_loader.cpp source.
  add_executable(test_interference
    tests/test_interference.cpp)
  target_include_directories(test_interference PRIVATE
    ${doctest_SOURCE_DIR})
  target_link_libraries(test_interference PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom)
  set_target_properties(test_interference PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME interference COMMAND test_interference)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(interference PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- PLM graphical revision diff (doc_render_diff): asserts on the returned JSON AND on the
  # decoded PNG pixels (miniz inflate of encode_png_rgb's filter-none RGB), so the red/green
  # colour semantics and the lowered min_painted floor for a sparse changes_only render are
  # both proven. No host AICM loader / render include path needed, like test_interference.
  add_executable(test_render_diff
    tests/test_render_diff.cpp)
  target_include_directories(test_render_diff PRIVATE
    ${doctest_SOURCE_DIR})
  target_link_libraries(test_render_diff PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom chudware_miniz)
  set_target_properties(test_render_diff PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME render_diff COMMAND test_render_diff)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(render_diff PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- A5: hole feature compiler — drill simple/cbore/csink bores into a body ---
  # core/doc/document.cpp's hole branch drills hole(s) at position(s) on a FACE-
  # SELECTOR-resolved planar face, building a cylinder/cbore/csink tool per position
  # (geom/hole.cpp: make_hole_tool), unioning them, and cutting the union from the
  # body — the SolidWorks Hole Wizard multi-position behaviour, simplified (no thread
  # tables / cosmetic threads / grid / sketch positions), ported from
  # engine/doc/features/hole.py. The test round-trips four hole documents (simple-
  # through, cbore-through, csink-through, multi-position) through the host AICM loader
  # (the contract oracle) and asserts analytic, OCCT-version-independent invariants
  # (face counts incl. the bore cylinder / csink cone, bbox, mesh volume). Same wiring
  # as test_mirror; chudware_doc carries the OCCT toolkits transitively via chudware_geom_occt.
  add_executable(test_hole
    tests/test_hole.cpp)
  target_include_directories(test_hole PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render)
  target_link_libraries(test_hole PRIVATE
    chudware_doc chudware_geom_occt chudware_sketch chudware_geom chudware_util)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_hole PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render/aicm_loader.cpp)
  set_target_properties(test_hole PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME hole COMMAND test_hole)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(hole PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Wave 2 milestone: the SELECTOR system — stable refs into B-rep topology --
  # core/doc/selectors.{h,cpp} turns picked topology into a geometric fingerprint
  # (kind/surf/centroid/axis/area/owner/hint) and re-resolves it against rebuilt
  # topology with the §3 scored best-match (margin rule refuses to guess on
  # symmetric ambiguity). The test has two groups: PURE resolution (hand-built
  # TopoInfo proving §3/§5 with hand-computed scores) and OCCT describers + an
  # end-to-end resolve that survives a dimension change. No host AICM loader
  # needed; links chudware_doc (carries selectors.cpp) + the OCCT kernel.
  add_executable(test_selectors
    tests/test_selectors.cpp)
  target_include_directories(test_selectors PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_selectors PRIVATE
    chudware_doc chudware_geom_occt chudware_geom)
  set_target_properties(test_selectors PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME selectors COMMAND test_selectors)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(selectors PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- A2: edge-selector pick resolution (doc/picks.cpp) ---------------------
  # core/doc/picks.cpp resolves edge selectors against a live B-rep body (the two-
  # stage describe_edges -> hint-fallback resolve -> nearest-unique-edge mapping ported
  # from engine/doc/features/dress.py) so the fillet/chamfer compilers accept an EDGE
  # SELECTOR (not just the dir filter), and a dangling selector becomes a per-feature
  # error status. The test round-trips a fillet-by-selector document through the host
  # AICM loader (the contract oracle), exactly like test_document. Same wiring.
  add_executable(test_picks
    tests/test_picks.cpp)
  target_include_directories(test_picks PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render)
  target_link_libraries(test_picks PRIVATE
    chudware_doc chudware_geom_occt chudware_geom chudware_util)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_picks PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/viewport/render/aicm_loader.cpp)
  set_target_properties(test_picks PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME picks COMMAND test_picks)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(picks PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # T-BL28: ruled/fill/boundary surface ops must accept body-edge selectors (the
  # K_EDGE-well refs the ribbon emits), not just sketch refs. Drives compile_document
  # directly (no host loader needed — asserts on CompileResult status/body_count).
  add_executable(test_surfacing
    tests/test_surfacing.cpp)
  target_include_directories(test_surfacing PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_surfacing PRIVATE
    chudware_doc chudware_geom_occt chudware_geom)
  set_target_properties(test_surfacing PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME surfacing COMMAND test_surfacing)
  set_tests_properties(surfacing PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- libchudware: the C ABI SHARED library (the host-integration seam) ---------
  # api/libchudware.{h,cpp} exposes a flat C ABI (chudware_call/chudware_free/chudware_abi_version)
  # over chudware::doc::compile_document so the host can load this lib at runtime
  # (LoadLibrary on Windows / dlopen on Linux) and route ported methods to the in-process
  # C++ core (the strangler seam; see docs/CPP_MIGRATION.md). chudware_doc (STATIC) drags in
  # the whole OCCT geom kernel transitively. The Netgen FEA-mesh RPCs in libchudware.cpp are
  # compiled out unless CHUDWARE_WITH_NETGEN, so the lib builds without the mesher (geometry,
  # sketch, features, STEP/IGES/STL still work — those need only OCCT).
  # Build id (git describe) shared by libchudware (chudware_build_id export) and
  # chudware-hostd, so a matched-build engine + daemon report the SAME id (version
  # drain-replace). Seeded at configure time so the header always exists, then
  # REFRESHED at build time by the chudware_version_info target below — a
  # configure-time-only stamp goes stale on dev rebuilds and blinds the
  # daemon_bridge staleness check (mega-8 F7/F8 root cause).
  execute_process(COMMAND git describe --tags --always --dirty
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
    OUTPUT_VARIABLE CHUDWARE_HOSTD_BUILD_ID
    OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
  if(NOT CHUDWARE_HOSTD_BUILD_ID)
    set(CHUDWARE_HOSTD_BUILD_ID "unknown")
  endif()
  configure_file(hostd/version_info.h.in ${CMAKE_CURRENT_BINARY_DIR}/hostd_gen/version_info.h @ONLY)
  add_custom_target(chudware_version_info
    COMMAND ${CMAKE_COMMAND}
            -DSRC_DIR=${CMAKE_CURRENT_SOURCE_DIR}
            -DOUT_HEADER=${CMAKE_CURRENT_BINARY_DIR}/hostd_gen/version_info.h
            -P ${CMAKE_CURRENT_SOURCE_DIR}/cmake/gen_version_info.cmake
    BYPRODUCTS ${CMAKE_CURRENT_BINARY_DIR}/hostd_gen/version_info.h
    COMMENT "Refreshing build id (git describe)")

  add_library(chudware SHARED
    api/libchudware.cpp)
  target_include_directories(chudware PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
  target_include_directories(chudware PRIVATE ${CMAKE_CURRENT_BINARY_DIR}/hostd_gen)
  add_dependencies(chudware chudware_version_info)
  target_compile_definitions(chudware PRIVATE CHUDWARE_BUILD_DLL)
  target_link_libraries(chudware PRIVATE chudware_doc chudware_sim chudware_ecad chudware_cam chudware_agent chudware_parts chudware_sourcing chudware_firmware chudware_structural chudware_prof chudware_tolerance chudware_mechanical chudware_materials chudware_dfm chudware_piping chudware_hydraulic)
  if(CHUDWARE_WITH_NETGEN)
    target_link_libraries(chudware PRIVATE chudware_mesh)
    target_compile_definitions(chudware PRIVATE CHUDWARE_WITH_NETGEN)  # enable the mesh RPCs in libchudware.cpp
  endif()
  # Ship a usable PDB next to libchudware.dll WITHOUT dropping optimization, so the sampling
  # profiler resolves OUR frames to real names (OCCT stays module-level — no OCCT PDBs).
  if(MSVC)
    target_compile_options(chudware PRIVATE /Zi)
    target_link_options(chudware PRIVATE /DEBUG /OPT:REF /OPT:ICF)
  elseif(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    # Keep our symbols in the dynamic symbol table so the sampling profiler's dladdr() resolves
    # OUR frames to real names (OCCT's exported symbols already resolve). GCC/Clang export shared-
    # library symbols by default, but make it explicit + robust to any future -fvisibility=hidden.
    target_link_options(chudware PRIVATE -Wl,--export-dynamic)
  endif()
  set_target_properties(chudware PROPERTIES
    OUTPUT_NAME libchudware   # Windows: libchudware.dll. With PREFIX "" below, Linux: libchudware.so
    PREFIX ""              # don't prepend the usual lib -> the host loads it as "libchudware.{dll,so}"
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  # Stage the OCCT toolkit + 3rdparty DLLs next to libchudware (no-op on non-Windows: the
  # system loader resolves the .so dependencies).
  chudware_occt_copy_dlls(chudware)
  if(CHUDWARE_WITH_NETGEN)
    chudware_netgen_copy_dlls(chudware)  # nglib.dll + ngcore.dll + zlib.dll (chudware_mesh runtime deps)
  endif()
  if(CHUDWARE_WITH_LIB3MF)
    chudware_lib3mf_copy_dll(chudware)   # lib3mf.dll for the CppDynamic 3MF binding
  endif()

  # --- chudware_probe: load libchudware the way the HOST will (dlopen / LoadLibrary) -
  # Links NEITHER the core static libs NOR OCCT — only nlohmann_json. It loads
  # libchudware, exercises ping + doc_compile_cpp + the unknown-method fallback signal,
  # and asserts a real box compile produced AICM bytes — validating the entire
  # runtime-load + dependency-discovery story before any host code changes.
  # platform_paths.cpp is compiled in rather than linked from chudware_util so the
  # probe keeps its "no core static libs" property — it is the loader smoke test.
  add_executable(chudware_probe api/chudware_probe.cpp util/platform_paths.cpp)
  target_include_directories(chudware_probe PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
  target_link_libraries(chudware_probe PRIVATE nlohmann_json::nlohmann_json)
  set_target_properties(chudware_probe PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_dependencies(chudware_probe chudware)  # lib built (+ OCCT DLLs staged) before the probe runs
  add_test(NAME probe COMMAND chudware_probe)
  # Same OCCT DLL PATH prepend as the other OCCT tests, as a backup to the staged DLLs.
  set_tests_properties(probe PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- chudware-hostd: the shared engine-host daemon -------------------------------
  # Owns the ONE libchudware instance and serves it to the GUI + every MCP process
  # over local IPC (unix socket / named pipe). Like chudware_probe it dlopens the
  # engine (via services/worker/cpp_core.cpp) rather than linking it, so the
  # daemon binary is tiny, engine-version-agnostic, and reuses the exact loader
  # every other consumer runs. See the hostd plan + docs/HOSTD.md.
  # (Build id header configured above, next to the chudware target — shared.)
  # Both platform backends are always listed; each file self-selects via
  # #ifdef _WIN32 / #ifndef _WIN32, so there is one source list to maintain.
  add_executable(chudware-hostd
    hostd/main.cpp
    hostd/server.cpp
    hostd/worker_bridge.cpp           # lazy python worker for engine-unknown RPCs (web PLM/AI roles)
    hostd/child_supervisor.cpp        # POSIX: fork/PDEATHSIG/execvpe
    hostd/child_supervisor_win.cpp    # Windows: Job Objects, KILL_ON_JOB_CLOSE
    hostd/doc_registry.cpp
    hostd/autosave.cpp
    hostd/hostd_log.cpp
    # hostd/mfg_lane.cpp is compiled into chudware_manufacturing, not here, so the daemon
    # and test_mfg_lane share exactly one definition of the single-owner device lane.
    hostd/tray_icon.cpp               # optional Linux AppIndicator status cube
    ${CMAKE_CURRENT_SOURCE_DIR}/../services/ipc/ipc_transport.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../services/ipc/ipc_transport_win.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../services/ipc/ipc_discovery.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../services/ipc/ipc_discovery_win.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../services/ipc/ipc_client.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../services/worker/cpp_core.cpp)
  target_include_directories(chudware-hostd PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR} ${CMAKE_CURRENT_BINARY_DIR}/hostd_gen
    ${CHUDWARE_GENERATED_INCLUDE_DIR})   # api/chudware_version.h (product semver)
  # chudware_util: platform_paths (selfPath/pidAlive) + parent_death (the PDEATHSIG /
  # kqueue-babysitter guard child_supervisor.cpp and worker_bridge.cpp fork through).
  target_link_libraries(chudware-hostd PRIVATE
    nlohmann_json::nlohmann_json chudware_miniz chudware_manufacturing chudware_util)
  if(WIN32)
    # advapi32: pipe DACL (SetEntriesInAclW), token/SID (OpenProcessToken,
    # ConvertSidToStringSidW, CreateWellKnownSid). Everything else (named pipes,
    # Job Objects, CreateProcessW) is kernel32, linked by default.
    target_link_libraries(chudware-hostd PRIVATE advapi32)
  else()
    # Optional system tray (libappindicator3). Disable at runtime with
    # CHUDWARE_HOSTD_TRAY=0, or at configure time by leaving the package missing.
    find_package(PkgConfig QUIET)
    if(PkgConfig_FOUND)
      pkg_check_modules(APPINDICATOR QUIET appindicator3-0.1)
    endif()
    if(APPINDICATOR_FOUND)
      target_compile_definitions(chudware-hostd PRIVATE CHUDWARE_HOSTD_HAS_TRAY=1)
      target_include_directories(chudware-hostd PRIVATE ${APPINDICATOR_INCLUDE_DIRS})
      target_link_libraries(chudware-hostd PRIVATE ${APPINDICATOR_LIBRARIES})
      if(APPINDICATOR_CFLAGS_OTHER)
        target_compile_options(chudware-hostd PRIVATE ${APPINDICATOR_CFLAGS_OTHER})
      endif()
      message(STATUS "chudware-hostd: system tray enabled (appindicator3)")
    else()
      message(STATUS "chudware-hostd: system tray disabled (appindicator3 not found)")
    endif()
  endif()
  set_target_properties(chudware-hostd PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_dependencies(chudware-hostd chudware)  # engine staged before the daemon can serve it
endif()

# === 3D assembly mate solver (C++ port of engine/doc/mate_solver.py) ==========
# Standalone, OCCT-free: rigid-component placement over the shared solver core
# (chudware_solver) + Eigen + json. Mirrors the sketch lib/cli/diff-harness shape.
add_library(chudware_mate STATIC
  mate/mate.cpp
  # numpy_rng.cpp is a standalone (stdlib-only) bit-exact clone of numpy's
  # default_rng().standard_normal(); the antipodal-escape restart in mate.cpp draws from it
  # so the C++ random restarts land on the SAME points as the Python oracle. Compiled in
  # directly (not via chudware_sim) to keep chudware_mate OCCT-/sim-free.
  sim/numpy_rng.cpp)
target_include_directories(chudware_mate PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(chudware_mate PUBLIC chudware_solver Eigen3::Eigen nlohmann_json::nlohmann_json)

# mate_cli (differential-harness driver; see tools/mate_diff.py). Reads the
# {components, mates, resolved_geometry, warm?, drag?} fixture from argv[1] (or
# stdin if "-") and prints the solve_mates result as one JSON line.
add_executable(mate_cli
  mate/mate_cli.cpp)
target_link_libraries(mate_cli PRIVATE chudware_mate)
set_target_properties(mate_cli PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# Optional doctest smoke for the mate solver (rotation round-trips + the peg-in-hole
# scenario). Only built when doctest is available.
if(TARGET test_nsolve)
  add_executable(test_mate
    tests/test_mate.cpp)
  target_include_directories(test_mate PRIVATE ${doctest_SOURCE_DIR})
  target_link_libraries(test_mate PRIVATE chudware_mate)
  set_target_properties(test_mate PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME mate COMMAND test_mate)

  # Smoke-load every core/mate/corpus/*.json fixture through solve_mates_json (schema +
  # spot checks: peg-in-hole DOF, chain convergence, over-defined/redundant/suppressed).
  add_executable(test_mate_corpus
    tests/test_mate_corpus.cpp)
  target_include_directories(test_mate_corpus PRIVATE ${doctest_SOURCE_DIR})
  target_link_libraries(test_mate_corpus PRIVATE chudware_mate)
  set_target_properties(test_mate_corpus PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME mate_corpus COMMAND test_mate_corpus)

  # Motion inverse dynamics (T-FIN20): OCCT-free analytic V&V — pendulum + slider-crank
  # joint-reaction cross-checks against closed-form mechanics on synthetic trajectories.
  add_executable(test_motion_dynamics
    tests/test_motion_dynamics.cpp
    doc/motion_dynamics.cpp)
  target_include_directories(test_motion_dynamics PRIVATE
    ${doctest_SOURCE_DIR} ${CMAKE_CURRENT_SOURCE_DIR})
  target_link_libraries(test_motion_dynamics PRIVATE chudware_mate)
  set_target_properties(test_motion_dynamics PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME motion_dynamics COMMAND test_motion_dynamics)
endif()

# Manufacturing gateway foundation and pure Grbl codec. This library deliberately contains no
# transport implementation or physical execute path.
add_library(chudware_manufacturing STATIC
  device/grbl_protocol.cpp
  device/grbl_adapter.cpp
  device/emulator_transport.cpp
  manufacturing/journal.cpp
  manufacturing/result_registry.cpp
  manufacturing/invalidation.cpp
  manufacturing/tier0.cpp
  manufacturing/policy.cpp
  manufacturing/gates.cpp
  manufacturing/approval.cpp
  manufacturing/lease.cpp
  manufacturing/arbiter.cpp
  hostd/mfg_lane.cpp                # single-owner device lane behind the mfg_* RPCs
  # Real transports. Deny-by-default: every open is gated by the commissioning allowlist.
  ${CMAKE_CURRENT_SOURCE_DIR}/../services/manufacturing/gateway/device_allowlist.cpp
  ${CMAKE_CURRENT_SOURCE_DIR}/../services/manufacturing/gateway/transport_serial.cpp
  ${CMAKE_CURRENT_SOURCE_DIR}/../services/manufacturing/gateway/transport_tcp.cpp
  ${CMAKE_CURRENT_SOURCE_DIR}/../services/manufacturing/gateway/transport_ws.cpp)
target_compile_features(chudware_manufacturing PUBLIC cxx_std_20)
target_include_directories(chudware_manufacturing PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR} ${CMAKE_CURRENT_SOURCE_DIR}/../services)
find_package(Threads REQUIRED)
target_link_libraries(chudware_manufacturing PUBLIC
  nlohmann_json::nlohmann_json Threads::Threads)
if(WIN32)
  target_link_libraries(chudware_manufacturing PUBLIC ws2_32)
endif()

add_executable(test_mfg_foundation tests/test_mfg_foundation.cpp)
target_include_directories(test_mfg_foundation PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_mfg_foundation PRIVATE chudware_manufacturing)
set_target_properties(test_mfg_foundation PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_mfg_foundation COMMAND test_mfg_foundation)

add_executable(test_grbl_protocol tests/test_grbl_protocol.cpp)
target_include_directories(test_grbl_protocol PRIVATE ${doctest_SOURCE_DIR})
target_compile_definitions(test_grbl_protocol PRIVATE
  CHUDWARE_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/..")
target_link_libraries(test_grbl_protocol PRIVATE chudware_manufacturing)
set_target_properties(test_grbl_protocol PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME test_grbl_protocol COMMAND test_grbl_protocol)

# Deterministic engineering twin: integer-tick only, links the SHARED Grbl codec so program
# semantics cannot drift from the real device lane. Produces no execute-side authorization.
add_library(chudware_twin STATIC twin/twin.cpp)
target_compile_features(chudware_twin PUBLIC cxx_std_20)
target_link_libraries(chudware_twin PUBLIC chudware_manufacturing)

# Additive authoring / durable model. No transport, no upload, no start/pause/cancel.
add_library(chudware_print STATIC print/model.cpp)
target_compile_features(chudware_print PUBLIC cxx_std_20)
target_link_libraries(chudware_print PUBLIC chudware_manufacturing)

foreach(_mfg_test test_device_lane test_mfg_authorization test_twin test_print_model
                  test_mfg_lane test_transport_serial test_transport_net)
  add_executable(${_mfg_test} tests/${_mfg_test}.cpp)
  target_include_directories(${_mfg_test} PRIVATE ${doctest_SOURCE_DIR})
  target_compile_definitions(${_mfg_test} PRIVATE
    CHUDWARE_SOURCE_DIR="${CMAKE_CURRENT_SOURCE_DIR}/..")
  set_target_properties(${_mfg_test} PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME ${_mfg_test} COMMAND ${_mfg_test})
endforeach()
target_link_libraries(test_device_lane PRIVATE chudware_manufacturing)
target_link_libraries(test_mfg_authorization PRIVATE chudware_manufacturing)
target_link_libraries(test_twin PRIVATE chudware_twin)
target_link_libraries(test_print_model PRIVATE chudware_print)
target_link_libraries(test_mfg_lane PRIVATE chudware_manufacturing)
target_link_libraries(test_transport_serial PRIVATE chudware_manufacturing)
target_link_libraries(test_transport_net PRIVATE chudware_manufacturing)

# Turning + laser/plasma/waterjet process models. Reads the checked-in fixtures directly.
add_executable(test_cam_processes tests/test_cam_processes.cpp)
target_include_directories(test_cam_processes PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_cam_processes PRIVATE chudware_cam)
target_compile_definitions(test_cam_processes PRIVATE
  CHUDWARE_MFG_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/../fixtures/mfg")
set_target_properties(test_cam_processes PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME cam_processes COMMAND test_cam_processes)
