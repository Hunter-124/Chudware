# Linux aarch64 from an x86_64 Linux build host: the distribution's GNU cross
# compiler (Ubuntu g++-aarch64-linux-gnu, libraries under /usr/aarch64-linux-gnu)
# builds the core, and qemu-user runs its tests. This is emulation, not native
# ARM hardware: a green ctest here proves the aarch64 code paths, not an ARM
# machine's timing, memory model under real contention, or kernel.
#
# Every non-toolchain dependency (OCCT 7.9.3, FreeType, RapidJSON, fontconfig,
# OpenSSL, curl) comes from the aarch64 SDK scripts/build_arm_sdk.sh builds
# from pinned sources into a private prefix, passed as CHUDWARE_ARM_SDK (cache
# variable or environment). Package, library and header searches are confined
# to that prefix and the cross sysroot, so the build host's x86_64 OCCT in
# /usr/local or any host pkg-config entry can never leak into an aarch64 link.
#
# ctest launches every test through qemu-aarch64-run.sh (beside this file),
# which sets QEMU_LD_PREFIX and a private per-run TMPDIR. Tests that re-exec
# themselves (the AICM cross-process handoff) are started by the x86_64
# /bin/sh, so they also need the build host's binfmt_misc qemu-aarch64 entry.

set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(CMAKE_LIBRARY_ARCHITECTURE aarch64-linux-gnu)

set(CMAKE_C_COMPILER aarch64-linux-gnu-gcc)
set(CMAKE_CXX_COMPILER aarch64-linux-gnu-g++)

set(CHUDWARE_ARM_SYSROOT /usr/aarch64-linux-gnu CACHE PATH "GNU ARM runtime sysroot")

if(NOT CHUDWARE_ARM_SDK AND DEFINED ENV{CHUDWARE_ARM_SDK})
  set(CHUDWARE_ARM_SDK "$ENV{CHUDWARE_ARM_SDK}")
endif()
if(NOT CHUDWARE_ARM_SDK OR NOT IS_DIRECTORY "${CHUDWARE_ARM_SDK}")
  message(FATAL_ERROR
    "CHUDWARE_ARM_SDK must name the aarch64 SDK prefix printed by "
    "scripts/build_arm_sdk.sh linux-aarch64 (got '${CHUDWARE_ARM_SDK}')")
endif()
# try_compile projects re-read this file without the parent's cache.
list(APPEND CMAKE_TRY_COMPILE_PLATFORM_VARIABLES CHUDWARE_ARM_SDK CHUDWARE_ARM_SYSROOT)
list(PREPEND CMAKE_PREFIX_PATH "${CHUDWARE_ARM_SDK}")
set(CMAKE_FIND_USE_PACKAGE_REGISTRY OFF)
set(CMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY OFF)
set(CMAKE_BUILD_RPATH "${CHUDWARE_ARM_SDK}/lib")

find_program(CHUDWARE_QEMU_AARCH64 NAMES qemu-aarch64-static qemu-aarch64 REQUIRED)
set(CMAKE_CROSSCOMPILING_EMULATOR
  "${CMAKE_CURRENT_LIST_DIR}/qemu-aarch64-run.sh;${CHUDWARE_QEMU_AARCH64};${CHUDWARE_ARM_SYSROOT}")

set(CMAKE_FIND_ROOT_PATH "${CHUDWARE_ARM_SDK}" "${CHUDWARE_ARM_SYSROOT}")
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# pkg-config is a host program; point it at the SDK's .pc files only.
set(ENV{PKG_CONFIG_LIBDIR} "${CHUDWARE_ARM_SDK}/lib/pkgconfig")
set(ENV{PKG_CONFIG_PATH} "")
