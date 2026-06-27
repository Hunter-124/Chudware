# ProvisionOCCT.cmake — make the OCCT (Open CASCADE) 3D B-rep kernel available.
#
# Windows: mirrors the app's runtime-provisioning philosophy (installer/provision.py
# for the Python runtime): if the kernel isn't already at the expected location,
# DOWNLOAD and extract the official prebuilt VC14 binary — no manual install step.
# Then find_package() it and expose a helper to deploy its DLLs next to an exe.
#
# Linux/macOS: there is no official prebuilt to download, so use the SYSTEM OCCT
# (e.g. Fedora `opencascade-devel`, Debian `libocct-*-dev`, Homebrew `opencascade`)
# via the standard CMake config search. Shared objects resolve through the system
# loader, so the DLL-staging helper is a no-op.
#
# Pinned to OCCT 7.9.3 on Windows (vc14/x64, VS2022-ABI-compatible). NOT 7.9.x->8.0:
# OCCT 8.0 changed BRepMesh_IncrementalMesh's API that our tessellation path depends
# on. On Linux we accept the distro's 7.9.x (Fedora 44 ships 7.9.3).

set(OCCT_VERSION "7.9.3" CACHE STRING "OCCT version to provision (Windows download)")

if(WIN32)
  set(OCCT_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/occt"
      CACHE PATH "OCCT install root (auto-provisioned if missing)")
  file(TO_CMAKE_PATH "${OCCT_ROOT}" OCCT_ROOT)

  set(_occt_home "${OCCT_ROOT}/opencascade-${OCCT_VERSION}-vc14-64")
  set(_occt_3p   "${OCCT_ROOT}/3rdparty-vc14-64")
  set(_occt_cfg  "${_occt_home}/cmake/OpenCASCADEConfig.cmake")

  # --- auto-provision if the kernel isn't present ----------------------------
  if(NOT EXISTS "${_occt_cfg}")
    set(_zip "${OCCT_ROOT}/occt-combined.zip")
    # The git tag uses underscores (V7_9_3) but the asset filename uses dots.
    string(REPLACE "." "_" _tag "V${OCCT_VERSION}")
    set(_url "https://github.com/Open-Cascade-SAS/OCCT/releases/download/${_tag}/opencascade-${OCCT_VERSION}-vc14-64-combined.zip")
    file(MAKE_DIRECTORY "${OCCT_ROOT}")
    if(NOT EXISTS "${_zip}")
      message(STATUS "[occt] kernel not detected — downloading prebuilt ${OCCT_VERSION} (~250 MB)…")
      file(DOWNLOAD "${_url}" "${_zip}" SHOW_PROGRESS STATUS _st)
      list(GET _st 0 _code)
      if(NOT _code EQUAL 0)
        file(REMOVE "${_zip}")
        message(FATAL_ERROR "[occt] download failed: ${_st}\n  url: ${_url}")
      endif()
    endif()
    message(STATUS "[occt] extracting ${_zip} → ${OCCT_ROOT} …")
    file(ARCHIVE_EXTRACT INPUT "${_zip}" DESTINATION "${OCCT_ROOT}")
  endif()
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
  # TKService) link transitively: the DLLs must be loadable for libaicad to load, even
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
  function(aicad_occt_copy_dlls tgt)
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
  function(aicad_occt_copy_dlls tgt)
    # no-op on non-Windows
  endfunction()
endif()
