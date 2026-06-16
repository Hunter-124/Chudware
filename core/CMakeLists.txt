cmake_minimum_required(VERSION 3.20)
project(aicad_core LANGUAGES CXX)

# The new pure-C++ engine (libaicad). Wave 1 = the constraint solver core; later
# waves add doc/geom (OCCT) and sim (CUDA). Built standalone here so the solver is
# the cleanest possible beachhead — no OCCT, no GUI, no FFI. The host will
# add_subdirectory(core) once a subsystem reaches parity (see docs plan).

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(MSVC)
  add_compile_options(/utf-8 /MP)
endif()

list(APPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake")

# Wave 2: provision + link the OCCT 3D B-rep kernel. OFF keeps the fast solver
# build (Wave 1) self-contained; -DAICAD_WITH_OCCT=ON auto-downloads OCCT and
# builds the geometry kernel + smoke test (see cmake/ProvisionOCCT.cmake).
option(AICAD_WITH_OCCT "Provision + link the OCCT geometry kernel (Wave 2)" OFF)

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

# --- libaicad solver ---------------------------------------------------------
add_library(aicad_solver STATIC
  solver/nsolve.cpp)
target_include_directories(aicad_solver PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(aicad_solver PUBLIC Eigen3::Eigen)

# --- libaicad geom (sketch-plane 2D<->3D lift + polar helpers) ---------------
add_library(aicad_geom STATIC
  geom/plane.cpp)
target_include_directories(aicad_geom PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(aicad_geom PUBLIC Eigen3::Eigen)

# --- libaicad util (dimension-expression arithmetic evaluator) ---------------
# C++ port of engine/doc/equations.py (the `evaluate` entry point) so the sketch
# solver's resolve_dim() matches the Python oracle for expr:"A*B"-style dims.
add_library(aicad_util STATIC
  util/expr.cpp)
target_include_directories(aicad_util PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})

# --- libaicad sketch (parameterization + constraints over the solver core) ---
add_library(aicad_sketch STATIC
  sketch/sketch.cpp)
target_include_directories(aicad_sketch PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(aicad_sketch PUBLIC aicad_solver aicad_util nlohmann_json::nlohmann_json)

# --- sketch_cli (differential-harness driver; see tools/solver_diff.py) -------
# Reads { "sketch":{...}, "env":{...} } from argv[1] (or stdin if "-") and prints
# the solve_sketch result as one JSON line. Output to bin/ alongside the tests.
add_executable(sketch_cli
  sketch/sketch_cli.cpp)
target_link_libraries(sketch_cli PRIVATE aicad_sketch)
set_target_properties(sketch_cli PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")

# --- tests -------------------------------------------------------------------
enable_testing()
add_executable(test_nsolve
  tests/test_nsolve.cpp)
target_include_directories(test_nsolve PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_nsolve PRIVATE aicad_solver)
set_target_properties(test_nsolve PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME nsolve COMMAND test_nsolve)

add_executable(test_sketch
  tests/test_sketch.cpp)
target_include_directories(test_sketch PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_sketch PRIVATE aicad_sketch)
set_target_properties(test_sketch PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME sketch COMMAND test_sketch)

add_executable(test_plane
  tests/test_plane.cpp)
target_include_directories(test_plane PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_plane PRIVATE aicad_geom)
set_target_properties(test_plane PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME plane COMMAND test_plane)

add_executable(test_expr
  tests/test_expr.cpp)
target_include_directories(test_expr PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_expr PRIVATE aicad_util)
set_target_properties(test_expr PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME expr COMMAND test_expr)

# --- libaicad geom-aicm (OCCT-free AICM v1 writer) ---------------------------
# The encoder half of engine/doc/tessellate.py, kept OCCT-free so it builds and
# unit-tests in the default (AICAD_WITH_OCCT=OFF) configuration too. The OCCT
# tessellator below links this same lib.
add_library(aicad_aicm STATIC
  geom/aicm.cpp)
target_include_directories(aicad_aicm PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})

add_executable(test_aicm
  tests/test_aicm_writer.cpp)
# Include the host loader's OWN directory (host/src/render), NOT host/src — the
# latter contains a project-local math.h that shadows the CRT <math.h> and breaks
# the standard <cmath>/<cstdlib> headers (MSVC C2039 'sinhf' etc.).
target_include_directories(test_aicm PRIVATE
  ${doctest_SOURCE_DIR}
  ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render)
target_link_libraries(test_aicm PRIVATE aicad_aicm)
target_sources(test_aicm PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render/aicm_loader.cpp)
set_target_properties(test_aicm PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME aicm COMMAND test_aicm)

# --- Wave 2: OCCT geometry kernel (opt-in) -----------------------------------
if(AICAD_WITH_OCCT)
  include(ProvisionOCCT)
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
  # planar faces (lifted via aicad_geom's SketchPlane), so this lib now also links
  # aicad_geom (SketchPlane/Eigen) and nlohmann_json.
  add_library(aicad_geom_occt STATIC
    geom/tessellate.cpp
    geom/features.cpp
    geom/profile.cpp
    geom/booleans.cpp
    geom/dress.cpp
    geom/aicm.cpp)
  target_include_directories(aicad_geom_occt PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  # Modelling toolkits for box/prism (TKPrim), revolve (TKPrim), polygon->face->wire
  # (TKBRep/TKTopAlgo), adaptors + GC_MakeArcOfCircle (TKGeomBase/TKGeomAlgo), mesh
  # (TKMesh), healing (TKShHealing). booleans.cpp adds Fuse/Cut/Common (TKBO) and
  # dress.cpp adds MakeFillet/MakeChamfer (TKFillet). aicad_geom brings SketchPlane +
  # Eigen3::Eigen; nlohmann_json for the solved-entity dicts profile.cpp consumes.
  target_link_libraries(aicad_geom_occt PUBLIC
    aicad_geom
    nlohmann_json::nlohmann_json
    TKernel TKMath TKG2d TKG3d TKGeomBase TKGeomAlgo
    TKBRep TKTopAlgo TKPrim TKMesh TKShHealing TKBO TKFillet)

  # Verification: tessellate -> write_aicm -> HOST render::parseAicm round-trip.
  add_executable(test_geom_aicm
    tests/test_geom_aicm.cpp)
  # host/src/render (the loader's dir) — NOT host/src, whose math.h shadows the
  # CRT <math.h> and breaks <cmath>/<cstdlib> under MSVC.
  target_include_directories(test_geom_aicm PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render)
  target_link_libraries(test_geom_aicm PRIVATE aicad_geom_occt)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_geom_aicm PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render/aicm_loader.cpp)
  set_target_properties(test_geom_aicm PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME geom_aicm COMMAND test_geom_aicm)
  # Same OCCT DLL PATH prepend as occt_smoke so the test can load TKernel et al.
  set_tests_properties(geom_aicm PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Wave 2 milestone: solved sketch -> profile -> extrude/revolve ---------
  # End-to-end: aicad::sketch::solve_sketch -> faces_from_solved_sketch ->
  # extrude_face/revolve_face -> tessellate -> write_aicm -> HOST render::parseAicm.
  # Links aicad_sketch for solve_sketch (aicad_geom_occt already brings the geom
  # kernel + aicad_geom + nlohmann_json transitively).
  add_executable(test_profile_extrude
    tests/test_profile_extrude.cpp)
  target_include_directories(test_profile_extrude PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render)
  target_link_libraries(test_profile_extrude PRIVATE
    aicad_geom_occt aicad_sketch aicad_geom)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_profile_extrude PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render/aicm_loader.cpp)
  set_target_properties(test_profile_extrude PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME profile_extrude COMMAND test_profile_extrude)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(profile_extrude PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Wave 2 milestone: boolean + dress-up ops compose into real parts ------
  # End-to-end: make_box/extrude_face base -> fuse/cut/common (booleans.h) +
  # fillet/chamfer (dress.h) -> tessellate -> write_aicm -> HOST render::parseAicm.
  # Same wiring as test_profile_extrude (aicad_geom_occt now carries TKBO/TKFillet).
  add_executable(test_ops
    tests/test_ops.cpp)
  target_include_directories(test_ops PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render)
  target_link_libraries(test_ops PRIVATE
    aicad_geom_occt aicad_sketch aicad_geom)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_ops PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render/aicm_loader.cpp)
  set_target_properties(test_ops PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME ops COMMAND test_ops)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(ops PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- Wave 2 milestone: the document model — a rebuildable feature tree ------
  # core/doc/document.cpp orchestrates solve_sketch -> faces_from_solved_sketch ->
  # extrude/revolve -> fuse/cut/common -> fillet/chamfer into a part, keyed by
  # feature id, and RE-RUNS on a parameter edit (the parametric payoff). It links
  # aicad_geom_occt (the OCCT kernel + aicad_geom + nlohmann_json, transitively),
  # aicad_sketch (solve_sketch), and aicad_util (the expr evaluator for expr dims).
  add_library(aicad_doc STATIC
    doc/document.cpp)
  target_include_directories(aicad_doc PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(aicad_doc PUBLIC
    aicad_geom_occt aicad_sketch aicad_geom aicad_util
    nlohmann_json::nlohmann_json)

  # Verification: compile a document -> write_aicm -> HOST render::parseAicm, with
  # the parametric-rebuild case (same doc, two envs -> two solids). Same wiring as
  # test_ops; aicad_doc carries the OCCT toolkits transitively via aicad_geom_occt.
  add_executable(test_document
    tests/test_document.cpp)
  target_include_directories(test_document PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render)
  target_link_libraries(test_document PRIVATE
    aicad_doc aicad_geom_occt aicad_sketch aicad_geom)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_document PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render/aicm_loader.cpp)
  set_target_properties(test_document PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME document COMMAND test_document)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(document PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")
endif()
