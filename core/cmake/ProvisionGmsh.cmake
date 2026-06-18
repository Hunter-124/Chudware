# ProvisionGmsh.cmake — make the gmsh meshing library available.
#
# Mirrors ProvisionOCCT.cmake's auto-provisioning philosophy: if the SDK isn't
# already extracted at the expected location, DOWNLOAD and extract the official
# prebuilt Windows SDK — no manual install step. Then expose the include dir, the
# import lib, the runtime DLL, and a helper to deploy the DLL next to an exe/dll.
#
# Pinned to gmsh 4.15.2 (the EXACT version the Python oracle in engine/sim/mesher.py
# imports via the `gmsh==4.15.2` wheel). Both sides wrap the identical libgmsh, so
# the tet mesh is bit-identical.
#
# LICENSE NOTE: gmsh is GPL-2+. This module is included ONLY inside the
# if(AICAD_WITH_OCCT) block, mirroring how the Python engine confines gmsh to
# engine/sim. Linking it into libaicad.dll makes that DLL a GPL derivative — the
# parent (build owner) decides on process/DLL isolation for distribution.

set(GMSH_VERSION "4.15.2" CACHE STRING "gmsh version to provision")
set(GMSH_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/gmsh"
    CACHE PATH "gmsh SDK root (auto-provisioned if missing)")
file(TO_CMAKE_PATH "${GMSH_ROOT}" GMSH_ROOT)

set(_gmsh_sdk "${GMSH_ROOT}/gmsh-${GMSH_VERSION}-Windows64-sdk")
set(_gmsh_hdr "${_gmsh_sdk}/include/gmsh.h")

# --- auto-provision if the SDK isn't present ---------------------------------
if(NOT EXISTS "${_gmsh_hdr}")
  set(_zip "${GMSH_ROOT}/gmsh-${GMSH_VERSION}-Windows64-sdk.zip")
  set(_url "https://gmsh.info/bin/Windows/gmsh-${GMSH_VERSION}-Windows64-sdk.zip")
  file(MAKE_DIRECTORY "${GMSH_ROOT}")
  if(NOT EXISTS "${_zip}")
    message(STATUS "[gmsh] SDK not detected — downloading prebuilt ${GMSH_VERSION} (~40 MB)…")
    file(DOWNLOAD "${_url}" "${_zip}" SHOW_PROGRESS STATUS _st)
    list(GET _st 0 _code)
    if(NOT _code EQUAL 0)
      file(REMOVE "${_zip}")
      message(FATAL_ERROR "[gmsh] download failed: ${_st}\n  url: ${_url}")
    endif()
  endif()
  message(STATUS "[gmsh] extracting ${_zip} → ${GMSH_ROOT} …")
  file(ARCHIVE_EXTRACT INPUT "${_zip}" DESTINATION "${GMSH_ROOT}")
endif()
if(NOT EXISTS "${_gmsh_hdr}")
  message(FATAL_ERROR "[gmsh] expected ${_gmsh_hdr} after provisioning — layout changed?")
endif()

# --- expose locations --------------------------------------------------------
set(GMSH_INCLUDE_DIR "${_gmsh_sdk}/include" CACHE PATH "gmsh SDK include dir" FORCE)

# The import lib lives in lib/. The 4.15.2 SDK names it `gmsh.dll.lib`; older/other
# SDKs use `gmsh.lib` — accept either. The runtime DLL is in lib/ too (named
# `gmsh-4.15.dll`), occasionally in bin/ — glob both to be robust.
find_file(GMSH_IMPORT_LIB
  NAMES gmsh.dll.lib gmsh.lib
  PATHS "${_gmsh_sdk}/lib" "${_gmsh_sdk}/bin"
  NO_DEFAULT_PATH)
if(NOT GMSH_IMPORT_LIB)
  message(FATAL_ERROR "[gmsh] import lib (gmsh.dll.lib / gmsh.lib) not found under ${_gmsh_sdk}/{lib,bin}")
endif()

set(_gmsh_dll "")
file(GLOB _gmsh_dll_lib "${_gmsh_sdk}/lib/gmsh*.dll")
file(GLOB _gmsh_dll_bin "${_gmsh_sdk}/bin/gmsh*.dll")
list(APPEND _gmsh_dll ${_gmsh_dll_lib} ${_gmsh_dll_bin})
if(NOT _gmsh_dll)
  message(FATAL_ERROR "[gmsh] gmsh.dll not found under ${_gmsh_sdk}/{lib,bin}")
endif()
list(GET _gmsh_dll 0 _gmsh_dll0)
set(GMSH_DLL "${_gmsh_dll0}" CACHE FILEPATH "gmsh runtime DLL" FORCE)

message(STATUS "[gmsh] ${GMSH_VERSION}  include=${GMSH_INCLUDE_DIR}")
message(STATUS "[gmsh]   import lib=${GMSH_IMPORT_LIB}")
message(STATUS "[gmsh]   runtime dll=${GMSH_DLL}")

# Deploy the gmsh runtime DLL next to a target's binary so libaicad.dll's import
# of gmsh.dll resolves when it is loaded from the same directory.
function(aicad_gmsh_copy_dll tgt)
  add_custom_command(TARGET ${tgt} POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different "${GMSH_DLL}" "$<TARGET_FILE_DIR:${tgt}>"
    COMMENT "[gmsh] copying gmsh.dll next to ${tgt}")
endfunction()
