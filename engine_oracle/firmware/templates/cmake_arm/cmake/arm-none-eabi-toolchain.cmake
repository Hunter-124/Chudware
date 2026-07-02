# CMake toolchain file for the arm-none-eabi (GNU Arm Embedded) cross-compiler.
# Selected by CMakeToolchain.build() via -DCMAKE_TOOLCHAIN_FILE. arm-none-eabi-gcc must be on PATH.
# It is invoked purely as a subprocess (GPL-with-runtime-exception, GPL-clean by aggregation —
# see contracts/licenses.md and contracts/firmware_rpc.md §1).
set(CMAKE_SYSTEM_NAME Generic)
set(CMAKE_SYSTEM_PROCESSOR arm)

set(CMAKE_C_COMPILER   arm-none-eabi-gcc)
set(CMAKE_CXX_COMPILER arm-none-eabi-g++)
set(CMAKE_ASM_COMPILER arm-none-eabi-gcc)
set(CMAKE_OBJCOPY      arm-none-eabi-objcopy CACHE FILEPATH "")
set(CMAKE_SIZE         arm-none-eabi-size CACHE FILEPATH "")

# We are cross-compiling for bare metal: don't try to run/link test executables against a hosted libc.
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)

set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
