# host/cmake/cef.cmake — wire the CEF off-screen browser backend into Chudware.
#
# Included ONLY when CHUDWARE_ENABLE_CEF=ON (see host/CMakeLists.txt). Follows the
# canonical CEF binary-distribution integration (cefsimple pattern): find the
# SDK, build libcef_dll_wrapper from source, add the libcef_lib logical target,
# compile the cef/ backend into Chudware + the subprocess helper, and stage the
# CEF runtime (libcef.so + *.pak + locales + helper) next to the binary.
#
#   cmake -S host -B host/build-cef -DCHUDWARE_ENABLE_CEF=ON \
#         -DCEF_ROOT=extern/cef/cef_binary_..._linux64_minimal
#
# CEF_ROOT defaults to the checked-out SDK under extern/cef if unset. Its Chromium
# version must match the Playwright-bundled Chromium (149 — see stealth.py) so
# cookies interoperate.

if(NOT DEFINED CEF_ROOT OR CEF_ROOT STREQUAL "")
  file(GLOB _cef_candidates "${CMAKE_CURRENT_SOURCE_DIR}/../extern/cef/cef_binary_*")
  if(_cef_candidates)
    list(GET _cef_candidates 0 CEF_ROOT)
  endif()
endif()
if(NOT CEF_ROOT OR NOT EXISTS "${CEF_ROOT}")
  message(FATAL_ERROR
    "CHUDWARE_ENABLE_CEF=ON but no CEF SDK found. Download a CEF binary distribution "
    "(Chromium ~149 to match the scraper) into extern/cef/ or pass -DCEF_ROOT=<dir>. "
    "See docs/cef_webview.md.")
endif()
# CEF's cmake macros embed CEF_LIB_* into set_target_properties strings. Windows
# backslashes are then parsed as CMake escapes (\U, \Y, …) and fail configure.
# Force forward slashes before find_package(CEF).
file(TO_CMAKE_PATH "${CEF_ROOT}" CEF_ROOT)

# The minimal distribution ships Release binaries only — build CEF bits Release.
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

# Chudware (and the rest of the host) uses the dynamic CRT (/MD). CEF's stock
# cmake defaults the wrapper to /MT (sandbox-oriented). With no_sandbox we must
# match /MD or the final link fails with LNK2038 RuntimeLibrary mismatch.
if(MSVC)
  set(CEF_RUNTIME_LIBRARY_FLAG "/MD" CACHE STRING "CEF CRT flag — must match host (/MD)" FORCE)
endif()

list(APPEND CMAKE_MODULE_PATH "${CEF_ROOT}/cmake")
set(CEF_ROOT "${CEF_ROOT}" CACHE INTERNAL "" FORCE)
find_package(CEF REQUIRED)                       # sets CEF_* vars + macros
add_subdirectory("${CEF_ROOT}/libcef_dll" libcef_dll_wrapper)
if(MSVC AND TARGET libcef_dll_wrapper)
  # Belt-and-suspenders: force dynamic CRT even if CEF macros re-inject /MT.
  set_property(TARGET libcef_dll_wrapper PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL")
  # Strip any leftover /MT from COMPILE_OPTIONS that CEF injects per-file.
  get_target_property(_cef_opts libcef_dll_wrapper COMPILE_OPTIONS)
  if(_cef_opts)
    list(FILTER _cef_opts EXCLUDE REGEX "^/MT")
    set_target_properties(libcef_dll_wrapper PROPERTIES COMPILE_OPTIONS "${_cef_opts}")
  endif()
endif()
add_logical_target("libcef_lib" "${CEF_LIB_DEBUG}" "${CEF_LIB_RELEASE}")
set_cef_target_out_dir()                         # → CEF_TARGET_OUT_DIR

# --- OSR backend sources compiled INTO Chudware (guarded by the compile define) ---
target_sources(Chudware PRIVATE
  src/ui/widgets/cef/cef_web_view.cpp
  src/ui/widgets/cef/cef_rpc_bridge.cpp        # AppState/RPC side — NO CEF headers, stays C++17
  src/ui/widgets/cef/cef_osr_shot.cpp          # headless OSR render-to-PNG (--cef-shot)
  src/ui/widgets/cef/cef_stealth.cpp           # shared UA/hints + JS hardening
  src/ui/widgets/cef/cef_headless.cpp          # headless fetch/download (--cef-fetch/--cef-download)
  src/ui/widgets/cef/cef_daemon.cpp            # CEF host daemon over chudware_ipc (--cef-daemon)
  src/ui/widgets/cef/cef_app.cpp)
if(WIN32)
  target_sources(Chudware PRIVATE src/ui/widgets/cef/cef_platform_windows.cpp)
else()
  target_sources(Chudware PRIVATE src/ui/widgets/cef/cef_platform_linux.cpp)
endif()

target_compile_definitions(Chudware PRIVATE CHUDWARE_ENABLE_CEF=1)
target_include_directories(Chudware PRIVATE "${CEF_ROOT}")
target_link_libraries(Chudware PRIVATE libcef_lib libcef_dll_wrapper ${CEF_STANDARD_LIBS})

# Load the STAGED libcef.so (next to the binary), not the SDK's build-time copy.
# CEF's Chrome runtime resolves icudtl.dat / *.pak relative to libcef.so's dir,
# and we stage those next to the binary — so libcef.so must load from $ORIGIN.
# (The helper already gets this via set_executable_target_properties.)
set_property(TARGET Chudware APPEND PROPERTY BUILD_RPATH "$ORIGIN")
set_property(TARGET Chudware APPEND PROPERTY INSTALL_RPATH "$ORIGIN")

# CEF 149 headers use C++20 concepts (std::same_as, …). The host is C++17; only
# the cef/*.cpp TUs include CEF headers, so bump JUST those to C++20 (ABI-
# compatible with the C++17 objects at link time). cef_web_view.h itself is
# CEF-free, so nothing else needs bumping.
if(MSVC)
  set(_CEF_CXX20 "/std:c++20")
else()
  set(_CEF_CXX20 "-std=c++20")
endif()
set_source_files_properties(
  src/ui/widgets/cef/cef_web_view.cpp
  src/ui/widgets/cef/cef_osr_shot.cpp
  src/ui/widgets/cef/cef_stealth.cpp
  src/ui/widgets/cef/cef_headless.cpp
  src/ui/widgets/cef/cef_daemon.cpp
  src/ui/widgets/cef/cef_app.cpp
  src/ui/widgets/cef/cef_platform_linux.cpp
  src/ui/widgets/cef/cef_platform_windows.cpp
  PROPERTIES COMPILE_OPTIONS "${_CEF_CXX20}")

# --- mandatory subprocess helper (its own executable) ---------------------------
# CEF's render/GPU/utility processes re-exec this; CefExecuteProcess() dispatches
# them. Must sit next to Chudware at runtime.
add_executable(chudware_cef_helper
  src/ui/widgets/cef/cef_helper_main.cpp
  src/ui/widgets/cef/cef_app.cpp
  src/ui/widgets/cef/cef_stealth.cpp   # render-process OnContextCreated injects this
  ../core/util/platform_paths.cpp)     # executable_dir() in the platform seam
if(WIN32)
  target_sources(chudware_cef_helper PRIVATE src/ui/widgets/cef/cef_platform_windows.cpp)
else()
  target_sources(chudware_cef_helper PRIVATE src/ui/widgets/cef/cef_platform_linux.cpp)
endif()
set_executable_target_properties(chudware_cef_helper)
target_compile_definitions(chudware_cef_helper PRIVATE CHUDWARE_ENABLE_CEF=1)
target_include_directories(chudware_cef_helper PRIVATE
  "${CEF_ROOT}" ${CMAKE_CURRENT_SOURCE_DIR}/src ${CMAKE_CURRENT_SOURCE_DIR}/../core)
target_link_libraries(chudware_cef_helper libcef_lib libcef_dll_wrapper ${CEF_STANDARD_LIBS})
# Helper TUs also include CEF headers (C++20 concepts on CEF 149+).
set_source_files_properties(
  src/ui/widgets/cef/cef_helper_main.cpp
  src/ui/widgets/cef/cef_app.cpp
  src/ui/widgets/cef/cef_stealth.cpp
  src/ui/widgets/cef/cef_platform_linux.cpp
  src/ui/widgets/cef/cef_platform_windows.cpp
  TARGET_DIRECTORY chudware_cef_helper
  PROPERTIES COMPILE_OPTIONS "${_CEF_CXX20}")

# --- stage the CEF runtime next to Chudware ---------------------------------------
set(CEF_TARGET_OUT_DIR "$<TARGET_FILE_DIR:Chudware>")
copy_files(Chudware "${CEF_BINARY_FILES}"   "${CEF_BINARY_DIR}"   "${CEF_TARGET_OUT_DIR}")
copy_files(Chudware "${CEF_RESOURCE_FILES}" "${CEF_RESOURCE_DIR}" "${CEF_TARGET_OUT_DIR}")
add_custom_command(TARGET Chudware POST_BUILD
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
          "$<TARGET_FILE:chudware_cef_helper>" "${CEF_TARGET_OUT_DIR}")

message(STATUS "Chudware: CEF backend enabled (CEF_ROOT=${CEF_ROOT}).")
