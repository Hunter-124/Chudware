# ProvisionLib3mf.cmake — make the lib3mf 3MF library available.
#
# Authenticate the official pinned platform SDK archive, including cache reuse,
# and reconstruct its managed extraction before exposing headers/runtime paths.
# LIB3MF_ROOT stores archive bytes; LIB3MF_LOCAL_SOURCE_DIR explicitly selects a
# reviewed development-only SDK. Existing extracted directories are not provenance.
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
# (unlike gmsh's GPL). No copyleft obligation on libchudware.dll.

set(LIB3MF_VERSION "2.5.0")
include("${CMAKE_CURRENT_LIST_DIR}/AuthenticatedArchive.cmake")
if(NOT DEFINED LIB3MF_LOCAL_SOURCE_DIR)
  set(LIB3MF_LOCAL_SOURCE_DIR "" CACHE PATH "Explicit reviewed local lib3mf SDK (development only)")
endif()
if(NOT DEFINED LIB3MF_ROOT)
  set(LIB3MF_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../third_party/lib3mf"
      CACHE PATH "lib3mf SDK root (auto-provisioned if missing)")
endif()
file(TO_CMAKE_PATH "${LIB3MF_ROOT}" LIB3MF_ROOT)

# lib3mf ships a per-platform SDK zip (same CppDynamic binding headers; the runtime is a
# .dll / .so / .dylib that the binding dlopens at runtime).
if(WIN32)
  set(_l3mf_plat "Windows")
  set(_l3mf_libpat "lib3mf*.dll")
  set(_l3mf_sha256 "8e185bc19e5d544c3c1e489fd8f8fcecfb0530def36e5c8bcc81bebf2ac96aa1")
elseif(APPLE)
  set(_l3mf_plat "Darwin")
  set(_l3mf_libpat "lib3mf*.dylib")
  set(_l3mf_sha256 "29e45884051e7f7d38dcc8922c0996dc8c60a3d6136d33d01976b3522e7d14eb")
else()
  set(_l3mf_plat "Linux")
  set(_l3mf_libpat "lib3mf*.so*")
  set(_l3mf_sha256 "658b40f83201dd172c8b3fe6bb4f21a047aa7e4125061399b6da9ea635ebda48")
endif()

if(LIB3MF_LOCAL_SOURCE_DIR)
  if(NOT CHUDWARE_DEVELOPER_BUILD OR DEFINED ENV{CI} OR DEFINED ENV{CHUDWARE_RELEASE_CHANNEL})
    message(FATAL_ERROR "[lib3mf] local SDK overrides are development-only")
  endif()
  set(_l3mf_install "${LIB3MF_LOCAL_SOURCE_DIR}")
  message(WARNING "[lib3mf] LOCAL SOURCE: ${_l3mf_install}; not an authenticated release SDK")
else()
  # Primary upstream release asset digests:
  # https://api.github.com/repos/3MFConsortium/lib3mf/releases/tags/v2.5.0
  set(_l3mf_install "${CMAKE_BINARY_DIR}/authenticated-sdk/lib3mf-${_l3mf_plat}")
  chudware_authenticated_archive(
    "https://github.com/3MFConsortium/lib3mf/releases/download/v2.5.0/lib3mf-2.5.0-${_l3mf_plat}.zip"
    "${LIB3MF_ROOT}/lib3mf-2.5.0-${_l3mf_plat}.zip"
    "${_l3mf_sha256}" "${_l3mf_install}")
endif()
set(_l3mf_sdk "${_l3mf_install}/lib3mf-${LIB3MF_VERSION}-${_l3mf_plat}")
set(_l3mf_hdr "${_l3mf_sdk}/include/Bindings/CppDynamic/lib3mf_dynamic.hpp")
if(NOT EXISTS "${_l3mf_hdr}")
  message(FATAL_ERROR "[lib3mf] expected ${_l3mf_hdr} after provisioning — layout changed?")
endif()

# --- expose locations --------------------------------------------------------
# The CppDynamic binding includes its siblings ("lib3mf_types.hpp",
# "lib3mf_dynamic.h") by relative name, so the include dir is that binding dir.
set(LIB3MF_INCLUDE_DIR "${_l3mf_sdk}/include/Bindings/CppDynamic"
    CACHE PATH "lib3mf CppDynamic binding include dir" FORCE)

# The runtime shared lib lives in bin/ (Windows) or lib/ (Linux/macOS). Glob both.
set(_l3mf_dll "")
file(GLOB _l3mf_dll_bin "${_l3mf_sdk}/bin/${_l3mf_libpat}")
file(GLOB _l3mf_dll_lib "${_l3mf_sdk}/lib/${_l3mf_libpat}")
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
# binding's CWrapper::loadLibrary("lib3mf.dll") resolves when libchudware.dll is
# loaded from the same directory.
function(chudware_lib3mf_copy_dll tgt)
  add_custom_command(TARGET ${tgt} POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different "${LIB3MF_DLL}" "$<TARGET_FILE_DIR:${tgt}>"
    COMMENT "[lib3mf] copying lib3mf.dll next to ${tgt}")
endfunction()
