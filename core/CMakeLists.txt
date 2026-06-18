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
add_library(aicad_miniz STATIC ${miniz_SOURCE_DIR}/miniz.c)
target_include_directories(aicad_miniz PUBLIC ${miniz_SOURCE_DIR})
# Match the dynamic CRT the rest of the core + the DLL use (silences LNK4098 and keeps
# miniz's malloc/free on the same heap as the DLL that calls mz_free).
set_property(TARGET aicad_miniz PROPERTY
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

# --- libaicad sim (simulation solvers) ---------------------------------------
# C++ port of engine/sim. FEA static linear elasticity first (tet4 + Eigen::SparseLU,
# the oracle is engine/sim/fea_static.py); modal / LBM / SPICE follow. OCCT-free —
# pure Eigen — so it builds in the standalone config too. CUDA acceleration is a
# later layer on top of these correct CPU cores.
add_library(aicad_sim STATIC
  sim/fea_static.cpp
  sim/fea_modal.cpp
  sim/spice.cpp
  sim/netlist.cpp
  sim/lbm.cpp)
target_include_directories(aicad_sim PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_include_directories(aicad_sim PRIVATE ${spectra_SOURCE_DIR}/include)
target_link_libraries(aicad_sim PUBLIC Eigen3::Eigen nlohmann_json::nlohmann_json)

# --- libaicad ecad (ECAD document model: schema + ops + session) -------------
# C++ port of engine/ecad (schema.py + ops.py + session.py). OCCT-free — the
# ECAD doc/ops layer is pure JSON (like aicad_sim) — so it builds in the
# standalone config and stays fast + isolated. Netlist/ERC/DRC/autoroute/Gerber/
# 3D are DEFERRED to later waves.
add_library(aicad_ecad STATIC
  ecad/schema.cpp
  ecad/ops.cpp
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
  ecad/session.cpp)
target_include_directories(aicad_ecad PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
# aicad_miniz is the ZIP read/write lib (shared with the .step+ container) — the
# .ecad+ container (container.cpp) writes/reads its archive through it.
target_link_libraries(aicad_ecad PUBLIC nlohmann_json::nlohmann_json aicad_miniz)

add_executable(test_sim tests/test_sim.cpp)
target_include_directories(test_sim PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_sim PRIVATE aicad_sim)
set_target_properties(test_sim PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME sim COMMAND test_sim)

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

add_executable(test_drag
  tests/test_drag.cpp)
target_include_directories(test_drag PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_drag PRIVATE aicad_sketch)
set_target_properties(test_drag PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME drag COMMAND test_drag)

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

# --- libaicad agent (LLM provider layer; WinHTTP TLS, OCCT-free) -------------
# P51: C++ port of agent/providers/*.py — the request builders + response parsers
# for Anthropic / OpenAI-compatible / llama.cpp / mock providers, plus a native
# WinHTTP POST (no libcurl/OpenSSL). Top-level (NOT inside if(AICAD_WITH_OCCT)) so
# it builds in both configs. Dynamic CRT to match aicad_miniz / the DLL heap.
add_library(aicad_agent STATIC
  agent/http.cpp
  agent/providers.cpp)
target_include_directories(aicad_agent PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(aicad_agent PUBLIC nlohmann_json::nlohmann_json winhttp)
set_property(TARGET aicad_agent PROPERTY
  MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")

add_executable(test_agent
  tests/test_agent.cpp)
target_include_directories(test_agent PRIVATE ${doctest_SOURCE_DIR})
target_link_libraries(test_agent PRIVATE aicad_agent)
set_target_properties(test_agent PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
add_test(NAME agent COMMAND test_agent)

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

  # --- P50: gmsh tet mesher (GPL — confined to the OCCT-on build) ------------
  # Provision the SAME libgmsh 4.15.2 the Python oracle (engine/sim/mesher.py)
  # wraps, so the C++ tet mesh is bit-identical. gmsh.h lives ONLY in mesher.cpp
  # (mesher.h is gmsh-free); aicad_mesh links the SDK import lib. The dynamic CRT
  # matches aicad_miniz / the DLL (same heap as the gmsh runtime).
  include(ProvisionGmsh)
  # P50b: sim/fea_run.cpp (the combined mesh+resolve+solve glue) joins the mesher
  # target — it is the only sim source that needs the mesher, and adding it here
  # keeps the gmsh confinement intact (fea_run.h itself is gmsh-free). aicad_mesh
  # gains a PUBLIC dep on aicad_sim so fea_run.cpp can call the FEA solvers.
  add_library(aicad_mesh STATIC sim/mesher.cpp sim/fea_run.cpp)
  target_include_directories(aicad_mesh PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
  target_include_directories(aicad_mesh PRIVATE ${GMSH_INCLUDE_DIR})
  # GMSH_DLL makes gmsh.h's GMSH_API decorate the prototypes __declspec(dllimport)
  # so they resolve against the SDK import lib (gmsh.dll.lib). PUBLIC so the import
  # lib propagates onto the final link line of the `aicad` DLL that consumes this
  # static lib (a STATIC lib's link deps aren't pulled in transitively otherwise).
  target_compile_definitions(aicad_mesh PRIVATE GMSH_DLL)
  target_link_libraries(aicad_mesh PUBLIC ${GMSH_IMPORT_LIB} aicad_sim nlohmann_json::nlohmann_json)
  set_property(TARGET aicad_mesh PROPERTY
    MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")
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
    geom/transform.cpp
    geom/hole.cpp
    geom/sheetmetal.cpp
    geom/weldment.cpp
    geom/surfacing.cpp
    geom/curves.cpp
    geom/ecad_3d.cpp
    doc/drawing.cpp
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
    aicad_ecad   # ecad/schema.h pad_world + ecad/ops.h OpError for geom/ecad_3d.cpp
    nlohmann_json::nlohmann_json
    TKernel TKMath TKG2d TKG3d TKGeomBase TKGeomAlgo
    TKBRep TKTopAlgo TKPrim TKMesh TKShHealing TKBO TKFillet
    # TKHLR: hidden-line removal (HLRBRep_Algo / HLRBRep_HLRToShape) for the 2D
    # drawing projection (doc/drawing.cpp; engine/doc/drawing.py oracle).
    TKHLR
    # TKOffset: shell (BRepOffsetAPI_MakeThickSolid) + draft (BRepOffsetAPI_DraftAngle)
    # + loft (BRepOffsetAPI_ThruSections).
    TKOffset
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
    # toolkit DLLs are already staged by aicad_occt_copy_dlls (copy_directory of the
    # whole OCCT bin dir).
    TKRWMesh TKDEOBJ TKDEGLTF TKXCAF TKLCAF TKCDF TKBin TKBinL TKBinXCAF)

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
    doc/document.cpp
    doc/selectors.cpp
    doc/picks.cpp
    doc/session.cpp
    doc/sketch_geom.cpp
    doc/stepplus.cpp
    doc/validate.cpp
    doc/formats.cpp
    doc/dxf.cpp
    doc/pdfvec.cpp
    doc/export2d.cpp)
  target_include_directories(aicad_doc PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(aicad_doc PUBLIC
    aicad_geom_occt aicad_sketch aicad_geom aicad_util
    aicad_miniz
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

  # --- A3: pattern feature compilers — linear_pattern / circular_pattern ------
  # core/doc/document.cpp's pattern branches REPLICATE a referenced seed solid with
  # rigid transforms (geom/transform.cpp: translate_copy / rotate_copy) and FUSE the
  # copies into one body (the geometry-pattern strategy of engine/doc/features/pattern.py,
  # in the simplified id-ref model). The test round-trips a linear + a circular pattern
  # document through the host AICM loader (the contract oracle) and asserts analytic,
  # OCCT-version-independent invariants (face counts, bbox, mesh volume). Same wiring as
  # test_document; aicad_doc carries the OCCT toolkits transitively via aicad_geom_occt.
  add_executable(test_pattern
    tests/test_pattern.cpp)
  target_include_directories(test_pattern PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render)
  target_link_libraries(test_pattern PRIVATE
    aicad_doc aicad_geom_occt aicad_sketch aicad_geom)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_pattern PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render/aicm_loader.cpp)
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
  # as test_pattern; aicad_doc carries the OCCT toolkits transitively via aicad_geom_occt.
  add_executable(test_mirror
    tests/test_mirror.cpp)
  target_include_directories(test_mirror PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render)
  target_link_libraries(test_mirror PRIVATE
    aicad_doc aicad_geom_occt aicad_sketch aicad_geom)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_mirror PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render/aicm_loader.cpp)
  set_target_properties(test_mirror PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME mirror COMMAND test_mirror)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(mirror PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

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
  # as test_mirror; aicad_doc carries the OCCT toolkits transitively via aicad_geom_occt.
  add_executable(test_hole
    tests/test_hole.cpp)
  target_include_directories(test_hole PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render)
  target_link_libraries(test_hole PRIVATE
    aicad_doc aicad_geom_occt aicad_sketch aicad_geom)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_hole PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render/aicm_loader.cpp)
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
  # needed; links aicad_doc (carries selectors.cpp) + the OCCT kernel.
  add_executable(test_selectors
    tests/test_selectors.cpp)
  target_include_directories(test_selectors PRIVATE
    ${doctest_SOURCE_DIR}
    ${OpenCASCADE_INCLUDE_DIR})
  target_link_libraries(test_selectors PRIVATE
    aicad_doc aicad_geom_occt aicad_geom)
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
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render)
  target_link_libraries(test_picks PRIVATE
    aicad_doc aicad_geom_occt aicad_geom)
  # Compile the host loader straight into the test target (the contract oracle).
  target_sources(test_picks PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/../host/src/render/aicm_loader.cpp)
  set_target_properties(test_picks PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_test(NAME picks COMMAND test_picks)
  # Same OCCT DLL PATH prepend so the test can load TKernel et al.
  set_tests_properties(picks PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")

  # --- libaicad: the C ABI SHARED library (the host-integration seam) ---------
  # api/libaicad.{h,cpp} exposes a flat C ABI (aicad_call/aicad_free/
  # aicad_abi_version) over aicad::doc::compile_document so the host can LoadLibrary
  # this DLL at runtime and route ported methods to the in-process C++ core (the
  # strangler seam; see docs/CPP_MIGRATION.md). Built as a SEPARATE libaicad.dll
  # (user's choice) so OCCT stays OUT of the host exe and the link is reversible.
  # aicad_doc (STATIC) drags in the whole OCCT geom kernel transitively, so the DLL
  # imports the TK* toolkit DLLs — staged next to it by aicad_occt_copy_dlls.
  add_library(aicad SHARED
    api/libaicad.cpp)
  target_include_directories(aicad PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
  target_compile_definitions(aicad PRIVATE AICAD_BUILD_DLL)
  target_link_libraries(aicad PRIVATE aicad_doc aicad_sim aicad_ecad aicad_mesh aicad_agent)
  set_target_properties(aicad PROPERTIES
    OUTPUT_NAME libaicad   # -> libaicad.dll + libaicad.lib (the host loads by this name)
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin"
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  # Stage the OCCT toolkit + 3rdparty (jemalloc/tbb) DLLs next to libaicad.dll so
  # its imports resolve when it is loaded from the same directory.
  aicad_occt_copy_dlls(aicad)
  # Stage gmsh.dll too (aicad_mesh's runtime dep, imported by libaicad.dll).
  aicad_gmsh_copy_dll(aicad)

  # --- aicad_probe: load libaicad.dll the way the HOST will (LoadLibrary) ------
  # Links NEITHER the core static libs NOR OCCT — only nlohmann_json. It dlopens
  # libaicad.dll, exercises ping + doc_compile_cpp + the unknown-method fallback
  # signal, and asserts a real box compile produced AICM bytes. This isolates and
  # validates the entire runtime-load + OCCT-DLL-discovery story before any host
  # code changes.
  add_executable(aicad_probe api/aicad_probe.cpp)
  target_include_directories(aicad_probe PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
  target_link_libraries(aicad_probe PRIVATE nlohmann_json::nlohmann_json)
  set_target_properties(aicad_probe PROPERTIES
    RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
  add_dependencies(aicad_probe aicad)  # DLL built + OCCT DLLs staged before the probe runs
  add_test(NAME probe COMMAND aicad_probe)
  # Same OCCT DLL PATH prepend as the other OCCT tests, as a backup to the staged DLLs.
  set_tests_properties(probe PROPERTIES ENVIRONMENT_MODIFICATION "${_occt_envmod}")
endif()
