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
