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
set(MCU_FLAGS "-mcpu=cortex-m4 -mthumb" CACHE STRING "MCU architecture flags")

add_compile_options(
    ${MCU_FLAGS}
    -ffunction-sections -fdata-sections
    -Wall -Wextra
    -Og -g
)
add_link_options(
    ${MCU_FLAGS}
    -Wl,--gc-sections
    -Wl,-Map=${CMAKE_PROJECT_NAME}.map
    --specs=nosys.specs
)

add_executable(${CMAKE_PROJECT_NAME}.elf
    src/main.c
    src/startup.c
)
target_include_directories(${CMAKE_PROJECT_NAME}.elf PRIVATE include)
