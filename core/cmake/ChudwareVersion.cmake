# Single source of truth for the Chudware PRODUCT version.
#
# Semver comes from annotated git tags shaped v<major>.<minor>.<patch>, read via
# `git describe --tags --always --dirty`. When no such tag is reachable the
# version is 0.0.0 with the real sha and CHUDWARE_BUILD_DIRTY 1, so an untagged
# working build can never be mistaken for a release.
#
# CONFIGURE time, deliberately — not build time. core/cmake/gen_version_info.cmake
# re-stamps hostd's BUILD ID on every build on purpose (daemon_bridge's staleness
# check needs a stamp that tracks the working tree). The PRODUCT version is the
# opposite case: chudware_version.h is included by the host GUI, the daemon and
# the About modal, so rewriting it per build would invalidate the compile cache on
# every commit for a string that only changes on a tag.
#
# One module, included by BOTH the root CMakeLists.txt and core/CMakeLists.txt
# (core/ is configured standalone with `-S core` for core/build-occt-linux, so the
# root file never runs for the daemon build). Two copies of the derive logic would
# drift and make a root build and a core build report different versions.
#
#   chudware_version_derive()
#       Sets CHUDWARE_VERSION, CHUDWARE_VERSION_{MAJOR,MINOR,PATCH},
#       CHUDWARE_GIT_SHA, CHUDWARE_BUILD_DIRTY and CHUDWARE_BUILD_DATE in the
#       calling scope. Runs git exactly once per configure regardless of how many
#       times it is called (root + core in one tree call it twice); the values are
#       memoised in INTERNAL cache entries refreshed on each configure run.
#
#   chudware_version_header(<out_dir>)
#       Writes <out_dir>/api/chudware_version.h. Put <out_dir> on a target's
#       include path and `#include "api/chudware_version.h"` resolves exactly the
#       way `#include "api/libchudware.h"` already does from the core source root.

set(_CHUDWARE_VERSION_MODULE_DIR "${CMAKE_CURRENT_LIST_DIR}")

macro(chudware_version_derive)
  # Re-derive once per configure run. A GLOBAL property (not a cache entry) is the
  # guard because it resets between runs, so a new commit is picked up; the values
  # themselves live in the cache so nested scopes and later includes share them.
  get_property(_cw_have GLOBAL PROPERTY CHUDWARE_VERSION_DERIVED)
  if(NOT _cw_have)
    set(_cw_major 0)
    set(_cw_minor 0)
    set(_cw_patch 0)
    set(_cw_dirty 1)
    set(_cw_sha "unknown")

    execute_process(COMMAND git describe --tags --always --dirty
      WORKING_DIRECTORY "${_CHUDWARE_VERSION_MODULE_DIR}"
      OUTPUT_VARIABLE _cw_desc OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
    execute_process(COMMAND git rev-parse --short HEAD
      WORKING_DIRECTORY "${_CHUDWARE_VERSION_MODULE_DIR}"
      OUTPUT_VARIABLE _cw_head OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)

    if(_cw_head)
      set(_cw_sha "${_cw_head}")
    elseif(_cw_desc MATCHES "^([0-9a-f]+)(-dirty)?$")
      # --always with no tag at all: describe IS the abbreviated sha.
      set(_cw_sha "${CMAKE_MATCH_1}")
    endif()

    if(_cw_desc)
      set(_cw_tag "${_cw_desc}")
      if(_cw_tag MATCHES "-dirty$")
        string(REGEX REPLACE "-dirty$" "" _cw_tag "${_cw_tag}")
        set(_cw_dirty 1)
      else()
        set(_cw_dirty 0)
      endif()
      if(_cw_tag MATCHES "^v([0-9]+)\\.([0-9]+)\\.([0-9]+)")
        set(_cw_major "${CMAKE_MATCH_1}")
        set(_cw_minor "${CMAKE_MATCH_2}")
        set(_cw_patch "${CMAKE_MATCH_3}")
      else()
        # No reachable v<x>.<y>.<z> tag (a non-semver tag, or none at all). 0.0.0
        # marked dirty: this build did not come from a release point, and nothing
        # downstream may treat it as one.
        set(_cw_dirty 1)
      endif()
    endif()

    string(TIMESTAMP _cw_date "%Y-%m-%d" UTC)

    set(CHUDWARE_VERSION_MAJOR "${_cw_major}" CACHE INTERNAL "Chudware major version" FORCE)
    set(CHUDWARE_VERSION_MINOR "${_cw_minor}" CACHE INTERNAL "Chudware minor version" FORCE)
    set(CHUDWARE_VERSION_PATCH "${_cw_patch}" CACHE INTERNAL "Chudware patch version" FORCE)
    set(CHUDWARE_VERSION "${_cw_major}.${_cw_minor}.${_cw_patch}"
        CACHE INTERNAL "Chudware semver" FORCE)
    set(CHUDWARE_GIT_SHA "${_cw_sha}" CACHE INTERNAL "Chudware build commit" FORCE)
    set(CHUDWARE_BUILD_DIRTY "${_cw_dirty}" CACHE INTERNAL "Chudware working tree dirty" FORCE)
    set(CHUDWARE_BUILD_DATE "${_cw_date}" CACHE INTERNAL "Chudware build date (UTC)" FORCE)

    set_property(GLOBAL PROPERTY CHUDWARE_VERSION_DERIVED 1)
    message(STATUS "Chudware version ${CHUDWARE_VERSION} (${CHUDWARE_GIT_SHA}, ${CHUDWARE_BUILD_DATE}, dirty=${CHUDWARE_BUILD_DIRTY})")
    unset(_cw_major)
    unset(_cw_minor)
    unset(_cw_patch)
    unset(_cw_dirty)
    unset(_cw_sha)
    unset(_cw_head)
    unset(_cw_desc)
    unset(_cw_tag)
    unset(_cw_date)
  endif()
  unset(_cw_have)
endmacro()

function(chudware_version_header out_dir)
  if(NOT DEFINED CHUDWARE_VERSION)
    message(FATAL_ERROR
      "chudware_version_header(): call chudware_version_derive() first")
  endif()
  # configure_file only rewrites when the content actually changed, so a
  # reconfigure on the same commit costs no recompiles.
  configure_file("${_CHUDWARE_VERSION_MODULE_DIR}/../api/chudware_version.h.in"
                 "${out_dir}/api/chudware_version.h" @ONLY)
endfunction()
