# macOS arm64 cross-compiled on an Intel Mac with the Command Line Tools'
# AppleClang and macOS SDK. COMPILE AND LINK ONLY: an Intel Mac cannot execute
# arm64 Mach-O, and nothing built with this file may be run or reported as tested.
# Native Apple-silicon runtime qualification stays
# on physical ARM hardware.
#
# Naming CMAKE_SYSTEM_NAME makes CMake treat the build as a cross build
# (CMAKE_CROSSCOMPILING, CMAKE_SYSTEM_PROCESSOR arm64) instead of an x86_64
# host build that merely passes -arch arm64. No CMAKE_CROSSCOMPILING_EMULATOR
# exists, so a ctest run fails loudly rather than pretending.
#
# OCCT 7.9.3, FreeType and RapidJSON come from the arm64 SDK
# scripts/build_arm_sdk.sh builds from pinned sources into a private prefix,
# passed as CHUDWARE_ARM_SDK. Library, header and package searches are
# confined to that prefix and the macOS SDK: the Intel Homebrew prefix holds
# x86_64-only libraries and must never satisfy an arm64 link.

set(CMAKE_SYSTEM_NAME Darwin)
set(CMAKE_SYSTEM_PROCESSOR arm64)
set(CMAKE_OSX_ARCHITECTURES arm64 CACHE STRING "Target architecture" FORCE)
set(CMAKE_C_COMPILER /usr/bin/clang)
set(CMAKE_CXX_COMPILER /usr/bin/clang++)

if(NOT CHUDWARE_ARM_SDK AND DEFINED ENV{CHUDWARE_ARM_SDK})
  set(CHUDWARE_ARM_SDK "$ENV{CHUDWARE_ARM_SDK}")
endif()
if(NOT CHUDWARE_ARM_SDK OR NOT IS_DIRECTORY "${CHUDWARE_ARM_SDK}")
  message(FATAL_ERROR
    "CHUDWARE_ARM_SDK must name the arm64 SDK prefix printed by "
    "scripts/build_arm_sdk.sh darwin-arm64 (got '${CHUDWARE_ARM_SDK}')")
endif()
list(APPEND CMAKE_TRY_COMPILE_PLATFORM_VARIABLES CHUDWARE_ARM_SDK CMAKE_OSX_SYSROOT)
list(PREPEND CMAKE_PREFIX_PATH "${CHUDWARE_ARM_SDK}")
set(CMAKE_FIND_USE_PACKAGE_REGISTRY OFF)
set(CMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY OFF)
set(CMAKE_BUILD_RPATH "${CHUDWARE_ARM_SDK}/lib")

if(NOT CMAKE_OSX_SYSROOT)
  execute_process(COMMAND xcrun --sdk macosx --show-sdk-path
    OUTPUT_VARIABLE _chudware_macos_sdk OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE _chudware_xcrun_rc TIMEOUT 30)
  if(NOT _chudware_xcrun_rc EQUAL 0 OR NOT IS_DIRECTORY "${_chudware_macos_sdk}")
    message(FATAL_ERROR "xcrun --sdk macosx --show-sdk-path found no macOS SDK")
  endif()
  set(CMAKE_OSX_SYSROOT "${_chudware_macos_sdk}" CACHE PATH "macOS SDK" FORCE)
endif()

set(CMAKE_FIND_ROOT_PATH "${CHUDWARE_ARM_SDK}" "${CMAKE_OSX_SYSROOT}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

set(ENV{PKG_CONFIG_LIBDIR} "${CHUDWARE_ARM_SDK}/lib/pkgconfig")
set(ENV{PKG_CONFIG_PATH} "")
