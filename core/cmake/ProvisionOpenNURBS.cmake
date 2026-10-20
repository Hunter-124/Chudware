# ProvisionOpenNURBS.cmake — build openNURBS (the C++ lib rhino3dm wraps) from
# source so the C++ core can read/write Rhino .3dm files at bit-parity with the
# Python oracle (engine/doc/formats.py, which uses the `rhino3dm` wheel).
#
# WHY FROM SOURCE (unlike lib3mf/gmsh which are prebuilt downloads): openNURBS
# ships no official prebuilt Windows static lib; it lives as a CMake-able
# submodule of mcneel/rhino3dm at src/lib/opennurbs with a clean static-lib
# target (`opennurbsStatic`). Fetch the exact rhino3dm and opennurbs commits,
# validate cached source against authenticated objects, then add_subdirectory
# ONLY a freshly reconstructed opennurbs tree — never python/wasm bindings.
# This builds opennurbsStatic plus its bundled zlib dependency.
#
# VERSION: rhino3dm 8.17.0 and its exact opennurbs submodule commit are pinned
# below, including on cache reuse. A reviewed local development tree must be
# selected explicitly through OPENNURBS_LOCAL_SOURCE_DIR.
#
# LICENSE NOTE: openNURBS is MIT-ish — McNeel's permissive "provided AS IS"
# toolkit license (+ bundled zlib's permissive license). No copyleft obligation
# on libchudware.dll (clean to link + distribute, unlike gmsh's GPL).
#
# OUTPUT: the imported/aliased target `opennurbsStatic` (PUBLIC include dir is the
# opennurbs source dir) and OPENNURBS_INCLUDE_DIR. The consumer (doc/formats.cpp,
# guarded by CHUDWARE_WITH_OPENNURBS) is the ONLY TU that includes opennurbs.h.

# Primary identities: https://api.github.com/repos/mcneel/rhino3dm/commits/8.17.0
# and /contents/src/lib/opennurbs?ref=1e0c80e6b9cf7ff3c1579d287e178b21634a2a6d
set(OPENNURBS_RHINO3DM_COMMIT "1e0c80e6b9cf7ff3c1579d287e178b21634a2a6d")
set(OPENNURBS_COMMIT "6c91d229ab561fe21c92ffdd97934c77ff945e0a")
if(NOT DEFINED OPENNURBS_LOCAL_SOURCE_DIR)
  set(OPENNURBS_LOCAL_SOURCE_DIR "" CACHE PATH "Reviewed local openNURBS source (development only)")
endif()
include("${CMAKE_CURRENT_LIST_DIR}/PinnedGitSource.cmake")
if(NOT DEFINED OPENNURBS_ROOT)
  set(OPENNURBS_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/third_party/rhino3dm-src"
      CACHE PATH "rhino3dm source checkout (auto-cloned if missing)")
endif()
file(TO_CMAKE_PATH "${OPENNURBS_ROOT}" OPENNURBS_ROOT)

if(OPENNURBS_LOCAL_SOURCE_DIR)
  if(NOT CHUDWARE_DEVELOPER_BUILD OR DEFINED ENV{CI} OR DEFINED ENV{CHUDWARE_RELEASE_CHANNEL})
    message(FATAL_ERROR "[opennurbs] local source overrides are development-only")
  endif()
  set(_on_dir "${OPENNURBS_LOCAL_SOURCE_DIR}")
  message(WARNING "[opennurbs] LOCAL SOURCE: ${_on_dir}; not authenticated release source")
else()
  # A managed checkout is never accepted merely because the headers exist.
  if(NOT EXISTS "${OPENNURBS_ROOT}/.git")
    if(EXISTS "${OPENNURBS_ROOT}/CMakeLists.txt")
      message(FATAL_ERROR "[opennurbs] unmanaged cached source rejected: ${OPENNURBS_ROOT}")
    endif()
    execute_process(COMMAND "${GIT_EXECUTABLE}" init "${OPENNURBS_ROOT}" RESULT_VARIABLE _git_rc)
    if(_git_rc EQUAL 0)
      execute_process(COMMAND "${GIT_EXECUTABLE}" --no-replace-objects -C "${OPENNURBS_ROOT}" -c http.sslVerify=true -c credential.helper= -c core.hooksPath= fetch --depth 1
        https://github.com/mcneel/rhino3dm.git "${OPENNURBS_RHINO3DM_COMMIT}" RESULT_VARIABLE _git_rc)
    endif()
    if(_git_rc EQUAL 0)
      execute_process(COMMAND "${GIT_EXECUTABLE}" --no-replace-objects -C "${OPENNURBS_ROOT}" -c core.hooksPath= checkout --detach
        "${OPENNURBS_RHINO3DM_COMMIT}" RESULT_VARIABLE _git_rc)
    endif()
    if(NOT _git_rc EQUAL 0)
      message(FATAL_ERROR "[opennurbs] pinned rhino3dm fetch failed: ${_git_rc}")
    endif()
  endif()
  chudware_require_pinned_git("${OPENNURBS_ROOT}" "${OPENNURBS_RHINO3DM_COMMIT}")
  set(_on_cached "${OPENNURBS_ROOT}/src/lib/opennurbs")
  if(NOT EXISTS "${_on_cached}/.git")
    # Repository URL is fixed here, not read from untrusted local Git configuration.
    execute_process(COMMAND "${GIT_EXECUTABLE}" --no-replace-objects -C "${OPENNURBS_ROOT}"
      -c http.sslVerify=true -c credential.helper= -c core.hooksPath=
      -c submodule.src/lib/opennurbs.url=https://github.com/mcneel/opennurbs.git
      submodule update --init --checkout --depth 1 src/lib/opennurbs RESULT_VARIABLE _git_rc)
    if(NOT _git_rc EQUAL 0)
      message(FATAL_ERROR "[opennurbs] pinned submodule fetch failed: ${_git_rc}")
    endif()
  endif()
  set(_on_dir "${CMAKE_BINARY_DIR}/authenticated-source/opennurbs")
  chudware_materialize_pinned_git("${_on_cached}" "${OPENNURBS_COMMIT}" "${_on_dir}")
endif()
if(NOT EXISTS "${_on_dir}/opennurbs.h" OR NOT EXISTS "${_on_dir}/CMakeLists.txt")
  message(FATAL_ERROR "[opennurbs] expected source headers and CMakeLists.txt under ${_on_dir}")
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
message(STATUS "[opennurbs] pinned ${OPENNURBS_COMMIT}  static target=opennurbsStatic")
message(STATUS "[opennurbs]   include=${OPENNURBS_INCLUDE_DIR}")
