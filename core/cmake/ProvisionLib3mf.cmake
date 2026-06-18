# ProvisionLib3mf.cmake — make the lib3mf 3MF library available.
#
# Mirrors ProvisionGmsh.cmake's auto-provisioning philosophy: if the SDK isn't
# already extracted at the expected location, DOWNLOAD and extract the official
# prebuilt Windows SDK — no manual install step. Then expose the include dir
# (the header-only C++ binding) and the runtime DLL, plus a helper to deploy the
# DLL next to an exe/dll.
#
# Pinned to lib3mf 2.5.0 — the EXACT version the Python oracle in
# engine/doc/formats.py imports via the `lib3mf==2.5.0` wheel. Both sides wrap
# the identical lib3mf.dll C ABI (the Python wheel dlopens it; the C++ side uses
# the CppDynamic header-only binding which ALSO dlopens it at runtime), so the
# 3MF mesh extraction (vertex coords + triangle indices) is bit-identical.
#
# BINDING CHOICE: the SDK ships several bindings under include/Bindings. We use
# the **CppDynamic** binding (lib3mf_dynamic.hpp): a header-only C++ wrapper that
# loads lib3mf.dll at runtime via LoadLibrary/GetProcAddress (CWrapper::
# loadLibrary). This needs NO import lib on the link line and is MSVC-safe (no
# symbol-name-mangling dependency on the DLL), exactly like the Python wheel's
# runtime dlopen. We therefore do NOT link lib3mf.lib — only stage lib3mf.dll
# next to the consuming binary so loadLibrary finds it.
#
# LICENSE NOTE: lib3mf is BSD-2-Clause — clean to vendor + link/distribute
# (unlike gmsh's GPL). No copyleft obligation on libaicad.dll.

set(LIB3MF_VERSION "2.5.0" CACHE STRING "lib3mf version to provision")
set(LIB3MF_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/lib3mf"
    CACHE PATH "lib3mf SDK root (auto-provisioned if missing)")
file(TO_CMAKE_PATH "${LIB3MF_ROOT}" LIB3MF_ROOT)

set(_l3mf_sdk "${LIB3MF_ROOT}/lib3mf-${LIB3MF_VERSION}-Windows")
set(_l3mf_hdr "${_l3mf_sdk}/include/Bindings/CppDynamic/lib3mf_dynamic.hpp")

# --- auto-provision if the SDK isn't present ---------------------------------
if(NOT EXISTS "${_l3mf_hdr}")
  set(_zip "${LIB3MF_ROOT}/lib3mf-${LIB3MF_VERSION}-Windows.zip")
  set(_url "https://github.com/3MFConsortium/lib3mf/releases/download/v${LIB3MF_VERSION}/lib3mf-${LIB3MF_VERSION}-Windows.zip")
  file(MAKE_DIRECTORY "${LIB3MF_ROOT}")
  if(NOT EXISTS "${_zip}")
    message(STATUS "[lib3mf] SDK not detected — downloading prebuilt ${LIB3MF_VERSION} (~5 MB)…")
    file(DOWNLOAD "${_url}" "${_zip}" SHOW_PROGRESS STATUS _st)
    list(GET _st 0 _code)
    if(NOT _code EQUAL 0)
      file(REMOVE "${_zip}")
      message(FATAL_ERROR "[lib3mf] download failed: ${_st}\n  url: ${_url}")
    endif()
  endif()
  message(STATUS "[lib3mf] extracting ${_zip} → ${LIB3MF_ROOT} …")
  file(ARCHIVE_EXTRACT INPUT "${_zip}" DESTINATION "${LIB3MF_ROOT}")
endif()
if(NOT EXISTS "${_l3mf_hdr}")
  message(FATAL_ERROR "[lib3mf] expected ${_l3mf_hdr} after provisioning — layout changed?")
endif()

# --- expose locations --------------------------------------------------------
# The CppDynamic binding includes its siblings ("lib3mf_types.hpp",
# "lib3mf_dynamic.h") by relative name, so the include dir is that binding dir.
set(LIB3MF_INCLUDE_DIR "${_l3mf_sdk}/include/Bindings/CppDynamic"
    CACHE PATH "lib3mf CppDynamic binding include dir" FORCE)

# The runtime DLL lives in bin/. Glob to be robust to a minor layout shuffle.
set(_l3mf_dll "")
file(GLOB _l3mf_dll_bin "${_l3mf_sdk}/bin/lib3mf*.dll")
file(GLOB _l3mf_dll_lib "${_l3mf_sdk}/lib/lib3mf*.dll")
list(APPEND _l3mf_dll ${_l3mf_dll_bin} ${_l3mf_dll_lib})
if(NOT _l3mf_dll)
  message(FATAL_ERROR "[lib3mf] lib3mf.dll not found under ${_l3mf_sdk}/{bin,lib}")
endif()
list(GET _l3mf_dll 0 _l3mf_dll0)
set(LIB3MF_DLL "${_l3mf_dll0}" CACHE FILEPATH "lib3mf runtime DLL" FORCE)

# The SDK ships an import lib (lib/lib3mf.lib) too, but the CppDynamic binding
# dlopens the DLL at runtime, so it is NOT needed on the link line. Expose it for
# completeness in case a consumer ever wants the non-dynamic (Cpp) binding.
find_file(LIB3MF_IMPORT_LIB
  NAMES lib3mf.lib
  PATHS "${_l3mf_sdk}/lib" "${_l3mf_sdk}/bin"
  NO_DEFAULT_PATH)

message(STATUS "[lib3mf] ${LIB3MF_VERSION}  include=${LIB3MF_INCLUDE_DIR}")
message(STATUS "[lib3mf]   runtime dll=${LIB3MF_DLL}")
if(LIB3MF_IMPORT_LIB)
  message(STATUS "[lib3mf]   import lib (unused, dynamic binding)=${LIB3MF_IMPORT_LIB}")
endif()

# Deploy the lib3mf runtime DLL next to a target's binary so the CppDynamic
# binding's CWrapper::loadLibrary("lib3mf.dll") resolves when libaicad.dll is
# loaded from the same directory.
function(aicad_lib3mf_copy_dll tgt)
  add_custom_command(TARGET ${tgt} POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different "${LIB3MF_DLL}" "$<TARGET_FILE_DIR:${tgt}>"
    COMMENT "[lib3mf] copying lib3mf.dll next to ${tgt}")
endfunction()
