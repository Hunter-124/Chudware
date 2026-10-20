# ProvisionOCCT.cmake — make the OCCT (Open CASCADE) 3D B-rep kernel available.
#
# Windows: authenticate the official pinned VC14 archive (including cache reuse),
# then reconstruct the managed SDK on every configure before find_package().
# Extracted-only caches are not trusted. OCCT_ROOT stores archive bytes;
# OCCT_LOCAL_SOURCE_DIR is the explicit reviewed development-only override.
#
# Linux/macOS: there is no official prebuilt to download, so use the SYSTEM OCCT
# (e.g. Fedora `opencascade-devel`, Debian `libocct-*-dev`, Homebrew `opencascade`)
# via the standard CMake config search. Shared objects resolve through the system
# loader, so the DLL-staging helper is a no-op.
#
# Cross presets (linux-aarch64-qemu, macos-arm64-cross) take the same Linux/macOS
# branch: their toolchain files (cmake/toolchains/) confine find_package to the
# target-architecture OCCT that scripts/build_arm_sdk.sh builds into CHUDWARE_ARM_SDK.
#
# Pinned to OCCT 7.9.3 on Windows (vc14/x64, VS2022-ABI-compatible). NOT 7.9.x->8.0:
# OCCT 8.0 changed BRepMesh_IncrementalMesh's API that our tessellation path depends
# on. On Linux we accept the distro's 7.9.x (Fedora 44 ships 7.9.3).

set(OCCT_VERSION "7.9.3")
include("${CMAKE_CURRENT_LIST_DIR}/AuthenticatedArchive.cmake")
if(NOT DEFINED OCCT_LOCAL_SOURCE_DIR)
  set(OCCT_LOCAL_SOURCE_DIR "" CACHE PATH "Explicit reviewed local OCCT SDK (development only)")
endif()

if(WIN32)
  if(NOT DEFINED OCCT_ROOT)
    set(OCCT_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/occt"
        CACHE PATH "OCCT install root (auto-provisioned if missing)")
  endif()
  file(TO_CMAKE_PATH "${OCCT_ROOT}" OCCT_ROOT)

  if(OCCT_LOCAL_SOURCE_DIR)
    if(NOT CHUDWARE_DEVELOPER_BUILD OR DEFINED ENV{CI} OR DEFINED ENV{CHUDWARE_RELEASE_CHANNEL})
      message(FATAL_ERROR "[occt] local SDK overrides are development-only")
    endif()
    set(_occt_install "${OCCT_LOCAL_SOURCE_DIR}")
    message(WARNING "[occt] LOCAL SOURCE: ${_occt_install}; not an authenticated release SDK")
  else()
    # Primary upstream release asset digest:
    # https://api.github.com/repos/Open-Cascade-SAS/OCCT/releases/tags/V7_9_3
    set(_occt_install "${CMAKE_BINARY_DIR}/authenticated-sdk/occt")
    chudware_authenticated_archive(
      "https://github.com/Open-Cascade-SAS/OCCT/releases/download/V7_9_3/opencascade-7.9.3-vc14-64-combined.zip"
      "${OCCT_ROOT}/occt-combined.zip"
      "afbef3457fbc4a2bdca0608e0fe284392f51e4c2c42ccbfb9df7168d8e4eb9b3"
      "${_occt_install}")
  endif()
  set(_occt_home "${_occt_install}/opencascade-${OCCT_VERSION}-vc14-64")
  set(_occt_3p   "${_occt_install}/3rdparty-vc14-64")
  set(_occt_cfg  "${_occt_home}/cmake/OpenCASCADEConfig.cmake")
  if(NOT EXISTS "${_occt_cfg}")
    message(FATAL_ERROR "[occt] expected ${_occt_cfg} after provisioning — layout changed?")
  endif()

  # --- find_package ----------------------------------------------------------
  set(OpenCASCADE_DIR "${_occt_home}/cmake")
  find_package(OpenCASCADE REQUIRED)
  message(STATUS "[occt] ${OpenCASCADE_VERSION} @ ${OpenCASCADE_DIR}")

  # --- runtime DLL locations (release variants) ------------------------------
  set(OCCT_DLL_DIR "${_occt_home}/win64/vc14/bin" CACHE PATH "OCCT toolkit DLLs" FORCE)
  # The 3rdparty deps our geom/mesh path actually loads. jemalloc + tbb are
  # TKernel's allocator/threading backends (mandatory); freetype/freeimage cover the
  # font/image paths used elsewhere. Note jemalloc's dir is named `jemalloc-vc14-64`.
  # ffmpeg (avcodec/avformat/avutil/swscale) + openvr are IMPORT-TABLE deps of TKService,
  # which the data-exchange toolkits (TKDESTEP/TKDEIGES/TKDESTL -> TKDE/TKXCAF -> ... ->
  # TKService) link transitively: the DLLs must be loadable for libchudware to load, even
  # though the headless import/export path never records video or drives a VR display.
  set(OCCT_3RDPARTY_BINS "")
  foreach(_dep jemalloc tbb freetype freeimage ffmpeg)
    file(GLOB _d "${_occt_3p}/${_dep}-*/bin")
    list(APPEND OCCT_3RDPARTY_BINS ${_d})
  endforeach()
  # openvr ships its DLL under bin/win64 (not bin) — glob that nested layout.
  file(GLOB _ovr "${_occt_3p}/openvr-*/bin/win64")
  list(APPEND OCCT_3RDPARTY_BINS ${_ovr})

  # Deploy the release DLLs next to a target's exe (for the shipped host later).
  function(chudware_occt_copy_dlls tgt)
    add_custom_command(TARGET ${tgt} POST_BUILD
      COMMAND ${CMAKE_COMMAND} -E copy_directory "${OCCT_DLL_DIR}" "$<TARGET_FILE_DIR:${tgt}>"
      COMMENT "[occt] copying toolkit DLLs next to ${tgt}")
    foreach(_bd ${OCCT_3RDPARTY_BINS})
      file(GLOB _dlls "${_bd}/*.dll")
      foreach(_d ${_dlls})
        get_filename_component(_n "${_d}" NAME)
        if(NOT _n MATCHES "_debug")
          add_custom_command(TARGET ${tgt} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different "${_d}" "$<TARGET_FILE_DIR:${tgt}>")
        endif()
      endforeach()
    endforeach()
  endfunction()

else()
  # --- Linux/macOS: system-provided OCCT -------------------------------------
  # The distro's CMake config (e.g. /usr/lib64/cmake/opencascade) defines the TK*
  # imported targets and OpenCASCADE_INCLUDE_DIR; the standard find_package search
  # locates it with no OpenCASCADE_DIR hint needed.
  find_package(OpenCASCADE REQUIRED)
  message(STATUS "[occt] system ${OpenCASCADE_VERSION} @ ${OpenCASCADE_DIR} (inc ${OpenCASCADE_INCLUDE_DIR})")

  # No DLL staging: OCCT + its 3rdparty libs resolve through the dynamic loader's
  # default search path. Leave the deploy helper as a no-op so call sites stay portable.
  set(OCCT_DLL_DIR "" CACHE PATH "OCCT toolkit DLLs (unused on non-Windows)" FORCE)
  set(OCCT_3RDPARTY_BINS "")
  function(chudware_occt_copy_dlls tgt)
    # no-op on non-Windows
  endfunction()
endif()
