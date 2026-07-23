# ProvisionOpenNURBS.cmake — build openNURBS (the C++ lib rhino3dm wraps) from
# source so the C++ core can read/write Rhino .3dm files at bit-parity with the
# Python oracle (engine/doc/formats.py, which uses the `rhino3dm` wheel).
#
# WHY FROM SOURCE (unlike lib3mf/gmsh which are prebuilt downloads): openNURBS
# ships no official prebuilt Windows static lib; it lives as a CMake-able
# submodule of mcneel/rhino3dm at src/lib/opennurbs with a clean static-lib
# target (`opennurbsStatic`). We FetchContent the rhino3dm repo (with submodules,
# so opennurbs + its bundled zlib are present) and add_subdirectory ONLY the
# opennurbs dir — never the python/wasm bindings. That builds opennurbsStatic
# (+ its sibling SHARED `OpenNURBS` and the bundled `zlib` target, which we leave
# unlinked).
#
# VERSION: pinned to rhino3dm v8.17.0 — the EXACT tag matching the `rhino3dm
# 8.17.0` wheel in the dev venv. At 8.17.0 opennurbs is the submodule commit
# 6c91d229 (mcneel/opennurbs). Reading STORED mesh data (ON_Mesh m_V/m_F) is
# archive-version-stable, but we pin anyway for faithfulness.
#
# LICENSE NOTE: openNURBS is MIT-ish — McNeel's permissive "provided AS IS"
# toolkit license (+ bundled zlib's permissive license). No copyleft obligation
# on libchudware.dll (clean to link + distribute, unlike gmsh's GPL).
#
# OUTPUT: the imported/aliased target `opennurbsStatic` (PUBLIC include dir is the
# opennurbs source dir) and OPENNURBS_INCLUDE_DIR. The consumer (doc/formats.cpp,
# guarded by CHUDWARE_WITH_OPENNURBS) is the ONLY TU that includes opennurbs.h.

set(OPENNURBS_RHINO3DM_TAG "8.17.0" CACHE STRING "rhino3dm git tag to provision opennurbs from")
set(OPENNURBS_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/third_party/rhino3dm-src"
    CACHE PATH "rhino3dm source checkout (auto-cloned if missing)")
file(TO_CMAKE_PATH "${OPENNURBS_ROOT}" OPENNURBS_ROOT)

set(_on_dir "${OPENNURBS_ROOT}/src/lib/opennurbs")
set(_on_hdr "${_on_dir}/opennurbs.h")
set(_on_cml "${_on_dir}/CMakeLists.txt")

# --- auto-provision the source if absent -------------------------------------
# Shallow clone the pinned tag WITH submodules (opennurbs + its bundled zlib are
# submodules of rhino3dm). We only need src/lib/opennurbs, but a recursive shallow
# clone is the simplest robust way to land it. If a partial/older checkout is
# present without the opennurbs CMakeLists, re-fetch the submodule.
if(NOT EXISTS "${_on_hdr}" OR NOT EXISTS "${_on_cml}")
  find_package(Git REQUIRED)
  if(NOT EXISTS "${OPENNURBS_ROOT}/.git")
    message(STATUS "[opennurbs] cloning rhino3dm ${OPENNURBS_RHINO3DM_TAG} (+submodules) …")
    execute_process(
      COMMAND ${GIT_EXECUTABLE} clone --depth 1 --branch ${OPENNURBS_RHINO3DM_TAG}
              --recurse-submodules --shallow-submodules
              https://github.com/mcneel/rhino3dm.git "${OPENNURBS_ROOT}"
      RESULT_VARIABLE _git_rc)
    if(NOT _git_rc EQUAL 0)
      message(FATAL_ERROR "[opennurbs] git clone of rhino3dm ${OPENNURBS_RHINO3DM_TAG} failed (rc=${_git_rc})")
    endif()
  else()
    message(STATUS "[opennurbs] checkout present — ensuring opennurbs submodule …")
    execute_process(
      COMMAND ${GIT_EXECUTABLE} -C "${OPENNURBS_ROOT}" submodule update --init --depth 1 src/lib/opennurbs
      RESULT_VARIABLE _git_rc)
    if(NOT _git_rc EQUAL 0)
      message(FATAL_ERROR "[opennurbs] submodule update for src/lib/opennurbs failed (rc=${_git_rc})")
    endif()
  endif()
endif()
if(NOT EXISTS "${_on_hdr}" OR NOT EXISTS "${_on_cml}")
  message(FATAL_ERROR "[opennurbs] expected ${_on_hdr} + ${_on_cml} after provisioning — layout changed?")
endif()

# --- build ONLY the opennurbs static lib -------------------------------------
# add_subdirectory pulls in opennurbs/CMakeLists.txt, which defines:
#   * opennurbsStatic (STATIC)  — what we link
#   * OpenNURBS       (SHARED)  — built but unlinked (harmless)
#   * zlib            (STATIC)  — bundled, linked transitively by opennurbsStatic
# Its own CMakeLists sets the MSVC defines (ON_COMPILING_OPENNURBS, UNICODE,
# Z_PREFIX, MY_ZCALLOC), /bigobj, and Win32 system libs (Shlwapi, Usp10). Those
# add_definitions/add_compile_options are scoped to this added subdirectory, so
# they do NOT leak into the rest of chudware_core.
if(NOT TARGET opennurbsStatic)
  # opennurbs (and its android_uuid subdir) carry cmake_minimum_required values below CMake
  # 3.5's floor, which CMake 4.x rejects outright. Relax the minimum for this subtree — set as
  # a normal variable before add_subdirectory so it propagates into the added scope only.
  set(CMAKE_POLICY_VERSION_MINIMUM 3.5)
  # opennurbs builds with /W4; do NOT let a parent -DWERROR turn its warnings fatal.
  add_subdirectory("${_on_dir}" "${CMAKE_BINARY_DIR}/opennurbs-build" EXCLUDE_FROM_ALL)
endif()

# Match the dynamic CRT the rest of core + the libchudware DLL use, so opennurbs's
# allocations live on the same heap and the link is /MD-consistent (no LNK4098).
foreach(_tgt opennurbsStatic OpenNURBS zlib)
  if(TARGET ${_tgt})
    set_property(TARGET ${_tgt} PROPERTY
      MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")
  endif()
endforeach()

set(OPENNURBS_INCLUDE_DIR "${_on_dir}" CACHE PATH "opennurbs include dir" FORCE)
message(STATUS "[opennurbs] from rhino3dm ${OPENNURBS_RHINO3DM_TAG}  static target=opennurbsStatic")
message(STATUS "[opennurbs]   include=${OPENNURBS_INCLUDE_DIR}")
