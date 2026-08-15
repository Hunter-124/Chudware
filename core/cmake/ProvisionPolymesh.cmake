# ProvisionPolymesh.cmake — make the Polyhedral-FEA (`polymesh`) mesher + adaptive
# FEA stack available as in-tree static libraries.
#
# Unlike OCCT / Netgen / lib3mf, polymesh ships NO install(), no export set and no
# package config: it is consumed by add_subdirectory only. It is also header+source
# pure C++20 with no runtime shared library, so there is nothing to install or stage —
# we clone a pinned commit under third_party/polymesh/src and add it to the build.
#
# Dependency sharing is the whole reason this file exists rather than a bare
# add_subdirectory: polymesh's own top level would `find_package(Eigen3)` and pick up
# the SYSTEM Eigen, while core/ builds against the FetchContent Eigen 3.4 pinned above.
# Eigen types cross the static-library boundary (polymesh::fea's public API is
# Eigen::VectorXd), so two Eigen versions in one link is an ODR violation, not a
# warning. Upstream now skips its find_package when the host has already defined
# Eigen3::Eigen / nlohmann_json::nlohmann_json — which core/CMakeLists.txt does before
# including this module. Keep that ordering.
#
# LICENSE NOTE: polymesh is BSD-3-Clause — the most permissive dependency in the tree.
# It links statically with no obligation beyond attribution, unlike Netgen (LGPL-2.1,
# dynamic-link-only) and CalculiX (GPL-2.0, subprocess-only).

# Pinned commit on https://github.com/Hunter-124/Polyhedral-FEA (master). A SHA, not a
# branch: this is a live first-party repository, and an unpinned clone would silently
# change the mesher under a reproducible build.
set(POLYMESH_COMMIT "aa5ae7627127dd22ef0d17d5cb2c3962156b9de0"
    CACHE STRING "Polyhedral-FEA commit to build")
set(POLYMESH_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/polymesh"
    CACHE PATH "Polyhedral-FEA source root (auto-provisioned if missing)")
file(TO_CMAKE_PATH "${POLYMESH_ROOT}" POLYMESH_ROOT)

set(_pm_src "${POLYMESH_ROOT}/src")

# --- clone the pinned commit if it isn't already present ---------------------
if(NOT EXISTS "${_pm_src}/CMakeLists.txt")
  find_package(Git REQUIRED)
  file(MAKE_DIRECTORY "${_pm_src}")
  message(STATUS "[polymesh] fetching Polyhedral-FEA @ ${POLYMESH_COMMIT}…")
  # A pinned SHA cannot be `git clone --branch`ed; fetch it by object id at depth 1
  # (GitHub serves reachable SHAs) so we pay for one commit, not 200 MB of history.
  execute_process(COMMAND "${GIT_EXECUTABLE}" init -q "${_pm_src}" RESULT_VARIABLE _pm_rc)
  if(_pm_rc EQUAL 0)
    execute_process(
      COMMAND "${GIT_EXECUTABLE}" -C "${_pm_src}" remote add origin
              https://github.com/Hunter-124/Polyhedral-FEA.git
      RESULT_VARIABLE _pm_rc)
  endif()
  if(_pm_rc EQUAL 0)
    execute_process(
      COMMAND "${GIT_EXECUTABLE}" -C "${_pm_src}" fetch --depth 1 origin "${POLYMESH_COMMIT}"
      RESULT_VARIABLE _pm_rc)
  endif()
  if(_pm_rc EQUAL 0)
    execute_process(
      COMMAND "${GIT_EXECUTABLE}" -C "${_pm_src}" checkout -q FETCH_HEAD
      RESULT_VARIABLE _pm_rc)
  endif()
  if(NOT _pm_rc EQUAL 0)
    message(FATAL_ERROR "[polymesh] fetch of ${POLYMESH_COMMIT} failed (${_pm_rc})")
  endif()
endif()

# --- embed configuration -----------------------------------------------------
# Every one of these OFF switches removes a FetchContent download: tests pull Catch2,
# the GUI pulls GLFW + Dear ImGui, and the advisor pulls a prebuilt ONNX Runtime that
# is not even SHA-pinned upstream. With them off the only externals polymesh needs are
# Eigen3, nlohmann_json and OCCT — all of which core/ already has.
set(POLYMESH_BUILD_TESTS  OFF CACHE BOOL "" FORCE)
set(POLYMESH_WITH_GUI     OFF CACHE BOOL "" FORCE)
set(POLYMESH_WITH_ADVISOR OFF CACHE BOOL "" FORCE)
set(POLYMESH_WITH_CUDA    OFF CACHE BOOL "" FORCE)
# polymesh compiles itself -Werror under -Wconversion. That is its own CI's business;
# a dependency must never be able to fail OUR build on a new compiler's new warning.
set(POLYMESH_WERROR       OFF CACHE BOOL "" FORCE)
# STEP/BREP reading is the entire point of the integration: the mesher consumes the
# same STEP file the Netgen path does, through the same OCCT this build already links.
set(POLYMESH_WITH_OCC     ON  CACHE BOOL "" FORCE)
if(DEFINED OpenCASCADE_DIR)
  set(OpenCASCADE_DIR "${OpenCASCADE_DIR}" CACHE PATH "" FORCE)
endif()

add_subdirectory("${_pm_src}" "${CMAKE_CURRENT_BINARY_DIR}/third_party/polymesh" EXCLUDE_FROM_ALL)

if(NOT TARGET polymesh::pipeline)
  message(FATAL_ERROR
    "[polymesh] target polymesh::pipeline missing after add_subdirectory — the pinned "
    "commit ${POLYMESH_COMMIT} predates the target-namespacing change?")
endif()

message(STATUS "[polymesh] ${POLYMESH_COMMIT}  src=${_pm_src} (BSD-3-Clause, static)")
