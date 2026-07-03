# Bare-metal ARM Cortex-M firmware — AI-CAD firmware template (cmake_arm baseline).
#
# Cross-compiled with arm-none-eabi via cmake/arm-none-eabi-toolchain.cmake. Configure with:
#   cmake -S . -B build -G Ninja \
#         -DCMAKE_TOOLCHAIN_FILE=cmake/arm-none-eabi-toolchain.cmake \
#         -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
# then: cmake --build build
cmake_minimum_required(VERSION 3.16)

# Emit compile_commands.json for clangd / IDE tooling (firmware_rpc.md §6). Set before project().
set(CMAKE_EXPORT_COMPILE_COMMANDS ON CACHE BOOL "" FORCE)

project(firmware C ASM)

set(CMAKE_C_STANDARD 11)

# MCU flags default to Cortex-M4; override -DMCU_FLAGS="..." for another core.
# T-C5 fix: this MUST be a CMake list (semicolon-separated), not a space-separated string — a
# CACHE STRING containing spaces is passed to add_compile_options()/add_link_options() as ONE
# argument, so GCC saw the literal token "-mcpu=cortex-m4 -mthumb" and rejected it outright
# (confirmed live: `arm-none-eabi-gcc: error: unrecognized -mcpu target: cortex-m4 -mthumb`) —
# this had never been build-tested against a real arm-none-eabi-gcc before T-C5.
set(MCU_FLAGS "-mcpu=cortex-m4;-mthumb" CACHE STRING "MCU architecture flags")

add_compile_options(
    ${MCU_FLAGS}
    -ffunction-sections -fdata-sections
    -Wall -Wextra
    -Og -g
)
# T-C5: linker.ld places .isr_vector/.text/.data/.bss at the addresses startup.c's Reset_Handler
# and this template's MCU_FLAGS/platform assume (default: STM32F4 map, matches
# templates/renode_platforms/stm32f4_discovery.repl). Without it (as before T-C5) the link
# succeeds but nothing can boot — there's no vector table placement or RAM/flash split.
add_link_options(
    ${MCU_FLAGS}
    -T${CMAKE_CURRENT_SOURCE_DIR}/linker.ld
    -Wl,--gc-sections
    -Wl,-Map=${CMAKE_PROJECT_NAME}.map
    --specs=nosys.specs
)

add_executable(${CMAKE_PROJECT_NAME}.elf
    src/main.c
    src/startup.c
)
# T-C5 fix: engine.firmware.pinmap.write_pinmap() writes the generated pin-map header to
# <project_root>/<build_dir>/include/pinmap.h — i.e. ${CMAKE_BINARY_DIR}/include when configured
# the documented way (`cmake -S . -B build`) — but that path was never added here, so any source
# that #includes "pinmap.h" failed to compile (confirmed live: `fatal error: pinmap.h: No such
# file or directory`). Static template headers (board.h) still live under the source-tree
# `include/`, so both paths are needed.
target_include_directories(${CMAKE_PROJECT_NAME}.elf PRIVATE include ${CMAKE_BINARY_DIR}/include)
set_target_properties(${CMAKE_PROJECT_NAME}.elf PROPERTIES LINK_DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/linker.ld)
