// Embedded firmware project templates (cmake_arm) + Renode platform files —
// generated from engine_oracle/firmware/templates (T-RF15 port; byte-identical
// contents). Regenerate by re-running the generator recorded in
// docs/REFACTOR_LOG.md if the Python template tree changes during the overlap window.
#include "firmware/templates.h"

namespace chudware::firmware {

const std::vector<TemplateFile>& cmake_arm_template_files() {
    static const std::vector<TemplateFile> kFiles = {
        {"CMakeLists.txt", R"CHUDWARE_TMPL(# Bare-metal ARM Cortex-M firmware — Chudware firmware template (cmake_arm baseline).
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
)CHUDWARE_TMPL"},
        {"cmake/arm-none-eabi-toolchain.cmake", R"CHUDWARE_TMPL(# CMake toolchain file for the arm-none-eabi (GNU Arm Embedded) cross-compiler.
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
)CHUDWARE_TMPL"},
        {"include/chudware_trace.h", R"CHUDWARE_TMPL(/* chudware_trace.h — firmware-side pin-event shim for the QEMU emulation fallback (T-FIN1).
 *
 * QEMU (mps2-an385) has no external GPIO observer the host can poll, so the firmware
 * reports its own pin transitions over a firmware-configured UART in a
 * machine-readable line parsed by the host:
 *
 *     CHUDWARE_GPIO_EVT <PIN>=<level> t=<ms>ms
 *
 * Usage (configure printf's UART driver; native semihosting is disabled):
 *
 *     #include "chudware_trace.h"
 *     CHUDWARE_GPIO_TRACE("PA5", 1, millis());   // pin name, level (0/1), timestamp in ms
 *     CHUDWARE_GPIO_TRACE_NT("PA5", 0);          // no-timestamp variant
 *
 * Both backends parse the same marker format. printf must use the selected
 * target's actual UART, not semihosting or another native-host service.
 */
#ifndef CHUDWARE_TRACE_H
#define CHUDWARE_TRACE_H

#include <stdio.h>

#define CHUDWARE_GPIO_TRACE(pin_str, level, t_ms) \
    printf("CHUDWARE_GPIO_EVT %s=%d t=%lums\n", (pin_str), ((level) ? 1 : 0), \
           (unsigned long)(t_ms))

#define CHUDWARE_GPIO_TRACE_NT(pin_str, level) \
    printf("CHUDWARE_GPIO_EVT %s=%d\n", (pin_str), ((level) ? 1 : 0))

/* CPU run/sleep residency markers (T-FIN7, optional). Print around sleep entry/exit
 * (e.g. before/after __WFI()) so the power-profile derivation (aicad.powerprofile/1)
 * can integrate measured run/sleep residency instead of assuming always-active:
 *
 *     CHUDWARE_CPU_TRACE("sleep", millis());  __WFI();  CHUDWARE_CPU_TRACE("run", millis());
 *
 * state_str must be "run" or "sleep". Both backends parse plain UART text;
 * the host parser derives the residency intervals. */
#define CHUDWARE_CPU_TRACE(state_str, t_ms) \
    printf("CHUDWARE_CPU_EVT %s t=%lums\n", (state_str), (unsigned long)(t_ms))

#endif /* CHUDWARE_TRACE_H */
)CHUDWARE_TMPL"},
        {"include/board.h", R"CHUDWARE_TMPL(/* Board/pin definitions — Chudware firmware template (cmake_arm baseline).
 *
 * When the project is linked to an ECAD board (board_doc_ref), the agent regenerates this header
 * from the board's net/pin assignments so firmware pin names track the schematic.
 */
#ifndef CHUDWARE_BOARD_H
#define CHUDWARE_BOARD_H

#define CHUDWARE_TEMPLATE_VERSION 1

#endif /* CHUDWARE_BOARD_H */
)CHUDWARE_TMPL"},
        {"linker.ld", R"CHUDWARE_TMPL(/* Minimal Cortex-M linker script — Chudware firmware template (cmake_arm baseline).
 *
 * Generic Cortex-M4 memory map (matches the STM32F4 family this template targets by default,
 * and Renode's templates/renode_platforms/stm32f4_discovery.repl: flash @ 0x08000000/1M,
 * sram @ 0x20000000/128K). Override FLASH/RAM origin+length for a different target family.
 */
MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 1M
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

ENTRY(Reset_Handler)

SECTIONS
{
    .isr_vector :
    {
        KEEP(*(.isr_vector))
    } > FLASH

    .text :
    {
        *(.text*)
        *(.rodata*)
    } > FLASH

    _sidata = LOADADDR(.data);

    .data :
    {
        _sdata = .;
        *(.data*)
        _edata = .;
    } > RAM AT> FLASH

    .bss :
    {
        _sbss = .;
        *(.bss*)
        *(COMMON)
        _ebss = .;
    } > RAM

    _estack = ORIGIN(RAM) + LENGTH(RAM);
}
)CHUDWARE_TMPL"},
        {"src/main.c", R"CHUDWARE_TMPL(/* Bare-metal blink skeleton — Chudware firmware template (cmake_arm baseline).
 *
 * Register addresses here are illustrative placeholders; the agent rewrites main() for the target
 * MCU/board (pulling pin assignments from the linked ECAD board_doc_ref when present). The point of
 * the template is a compilable starting tree that emits compile_commands.json, not a real BSP.
 */
#include <stdint.h>

/* Illustrative GPIO block — replaced per MCU. */
#define GPIO_BASE   0x40020000u
#define GPIO_ODR    (*(volatile uint32_t *)(GPIO_BASE + 0x14u))
#define LED_PIN     5u

static void delay(volatile uint32_t n)
{
    while (n--) {
        __asm__ volatile("nop");
    }
}

int main(void)
{
    for (;;) {
        GPIO_ODR ^= (1u << LED_PIN);  /* toggle LED */
        delay(1000000u);
    }
    return 0;
}
)CHUDWARE_TMPL"},
        {"src/startup.c", R"CHUDWARE_TMPL(/* Minimal reset/vector table — Chudware firmware template (cmake_arm baseline).
 *
 * A real target's CMSIS device pack supplies the full vector table (every NVIC IRQ) + a fuller
 * startup; this covers just the fixed Cortex-M core exceptions (enough to boot and run on any
 * Cortex-M without peripheral interrupts) so a configure/compile/link is genuinely load-complete
 * against linker.ld (T-C5: the prior version had no .isr_vector at all, so nothing could boot on
 * real hardware or in an emulator — this is the fix). Replaced/extended per MCU by the agent when
 * peripheral interrupts are needed.
 */
#include <stdint.h>

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack;
extern int main(void);

void Reset_Handler(void);
void Default_Handler(void);

/* Cortex-M core exceptions only (vector[0]=initial SP, vector[1]=Reset). NVIC peripheral IRQs
 * start at index 16 and are target-specific (CMSIS device header) — omitted here since this
 * template's main() polls peripherals rather than using interrupts. */
__attribute__((section(".isr_vector")))
void (* const g_pfnVectors[])(void) = {
    (void (*)(void))(&_estack),           /* initial stack pointer */
    Reset_Handler,                        /* Reset */
    Default_Handler,                      /* NMI */
    Default_Handler,                      /* HardFault */
    Default_Handler,                      /* MemManage */
    Default_Handler,                      /* BusFault */
    Default_Handler,                      /* UsageFault */
};

void Reset_Handler(void)
{
    uint32_t *src = &_sidata;
    uint32_t *dst = &_sdata;
    while (dst < &_edata) {
        *dst++ = *src++;
    }
    dst = &_sbss;
    while (dst < &_ebss) {
        *dst++ = 0;
    }

    main();
    for (;;) {
        /* main returned — spin */
    }
}

/* Default handler for any unpopulated exception vector. */
void Default_Handler(void)
{
    for (;;) {
    }
}
)CHUDWARE_TMPL"},
    };
    return kFiles;
}

const std::vector<TemplateFile>& renode_platform_files() {
    static const std::vector<TemplateFile> kFiles = {
        {"LICENSE", R"CHUDWARE_TMPL(MIT License

Copyright (c) Antmicro Ltd. and Renode contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Note: These platform description files are adapted from Renode's open-source
platform infrastructure (https://github.com/renode/renode-infrastructure),
used under the MIT License.  The Renode binary is invoked as a subprocess and
is not vendored or linked into this project.
)CHUDWARE_TMPL"},
        {"cortex_m_generic.repl", R"CHUDWARE_TMPL(// Generic Cortex-M platform description for Renode (MIT)
// Minimal: flash + SRAM + CPU + NVIC only; no other peripheral mapping.
// License: MIT — see templates/renode_platforms/LICENSE
//
// T-C5 fix: Renode's CPU.CortexM constructor requires an `nvic` (IRQControllers.NVIC) reference
// — this file never declared one, so `machine LoadPlatformDescription` failed outright
// ("Could not find suitable constructor... Could not find corresponding attribute for parameter
// 'nvic'"), confirmed live. Never actually loaded in Renode before T-C5 (T-C3's tests mock the
// subprocess). Syntax mirrors Renode's own bundled platforms/cpus/stm32f4.repl.

nvic: IRQControllers.NVIC @ sysbus 0xE000E000
    priorityMask: 0xF0
    systickFrequency: 72000000
    IRQ -> cpu@0

cpu: CPU.CortexM @ sysbus
    cpuType: "cortex-m4"
    nvic: nvic

flash: Memory.MappedMemory @ sysbus 0x08000000
    size: 0x100000

sram: Memory.MappedMemory @ sysbus 0x20000000
    size: 0x20000
)CHUDWARE_TMPL"},
        {"esp32_stub.repl", R"CHUDWARE_TMPL(// ESP32 stub platform description for Renode (MIT)
// License: MIT — see templates/renode_platforms/LICENSE
//
// T-FIN2 DECISION — ESP32 is BUILD + FLASH ONLY, no emulation.
// Renode's Xtensa/ESP32 support is experimental and ships no usable UART/GPIO
// peripheral models; QEMU-xtensa (espressif fork) is not packaged for our
// supported distros. platform_db reports esp32 with emulation="none" and
// fw_sim_run refuses honestly instead of pretending. The build (idf.py) and
// flash (esptool) paths remain fully supported — see provision.FW_FAMILY_RULES.
// This stub is kept only so a future Renode with real ESP32 support has a seat.

cpu0: CPU.Xtensa @ sysbus
    cpuType: "esp32"

flash: Memory.MappedMemory @ sysbus 0x3F400000
    size: 0x400000

sram: Memory.MappedMemory @ sysbus 0x3FFB0000
    size: 0x50000
)CHUDWARE_TMPL"},
        {"nrf52840.repl", R"CHUDWARE_TMPL(// nRF52840 platform description for Renode (MIT)
// Source: renode/renode platforms/cpus/nrf52840.repl (upstream master, fetched 2026-07-10)
// https://github.com/renode/renode/blob/master/platforms/cpus/nrf52840.repl
// License: MIT — see templates/renode_platforms/LICENSE and upstream renode LICENSE.
//
// T-FIN2: adopted upstream verbatim (UART0/1, GPIO0/1 + GPIOTE, TWI(I2C)0/1, SPI2,
// TIMER0-4, RTC0-2, radio, RNG, ...) with ONE local change: the ApplySVD (network SVD fetch)
// sysbus init line is removed (network fetch at load time; runs must be offline-safe).
// [~] live-verify: renode is not installed on this machine; structure tests only.
nvic: IRQControllers.NVIC @ sysbus 0xE000E000
    -> cpu@0

cpu: CPU.CortexM @ sysbus
    cpuType: "cortex-m4"
    nvic: nvic

uart0: UART.NRF52840_UART @ sysbus 0x40002000
    easyDMA: true
    -> nvic@2

uart1: UART.NRF52840_UART @ sysbus 0x40028000
    easyDMA: true
    -> nvic@40

rtc0: Timers.NRF52840_RTC @ sysbus 0x4000B000
    numberOfEvents: 3
    -> nvic@11

rtc1: Timers.NRF52840_RTC @ sysbus 0x40011000
    numberOfEvents: 3
    -> nvic@17

rtc2: Timers.NRF52840_RTC @ sysbus 0x40024000
    numberOfEvents: 4
    -> nvic@36

wdt: Timers.NRF52840_Watchdog @ sysbus 0x40010000
    -> nvic@16

ppi: Miscellaneous.NRF52840_PPI @ sysbus 0x4001F000

flash: Memory.MappedMemory @ sysbus 0x0
    size: 0x100000

ram: Memory.MappedMemory @ { sysbus 0x800000; sysbus 0x20000000 }
    size: 0x40000

clock: Miscellaneous.NRF_CLOCK @ sysbus 0x40000000
    -> nvic@0

twi0: I2C.NRF52840_I2C @ sysbus 0x40003000
    -> nvic@0x3

spi2: SPI.NRF52840_SPI @ sysbus 0x40023000
    -> nvic@0x23

gpio0: GPIOPort.NRF52840_GPIO @ sysbus 0x50000500

gpio1: GPIOPort.NRF52840_GPIO @ sysbus 0x50000800

gpiote: GPIOPort.NRF52840_GPIOTasksEvents @ sysbus 0x40006000
    port0: gpio0
    port1: gpio1
    -> nvic@6

twi1: I2C.NRF52840_I2C @ sysbus 0x40004000
    -> nvic@4

timer0: Timers.NRF52840_Timer @ sysbus 0x40008000
    numberOfEvents: 4
    -> nvic@8

timer1: Timers.NRF52840_Timer @ sysbus 0x40009000
    numberOfEvents: 4
    -> nvic@9

timer2: Timers.NRF52840_Timer @ sysbus 0x4000A000
    numberOfEvents: 4
    -> nvic@10

timer3: Timers.NRF52840_Timer @ sysbus 0x4001A000
    numberOfEvents: 6
    -> nvic@26

timer4: Timers.NRF52840_Timer @ sysbus 0x4001B000
    numberOfEvents: 6
    -> nvic@27

i2s: Sound.NRF52840_I2S @ sysbus 0x40025000
    -> nvic@37

pdm: Sound.NRF52840_PDM @ sysbus 0x4001D000
    -> nvic@29

radio: Wireless.NRF52840_Radio @ sysbus 0x40001000
    -> nvic@1

rng: Miscellaneous.NRF52840_RNG @ sysbus 0x4000D000
    -> nvic@13

ecb: Miscellaneous.NRF52840_ECB @ sysbus 0x4000E000
    -> nvic@14

egu0 : Miscellaneous.NRF52840_EGU @ sysbus 0x40014000
    -> nvic@20

egu1 : Miscellaneous.NRF52840_EGU @ sysbus 0x40015000
    -> nvic@21

egu2 : Miscellaneous.NRF52840_EGU @ sysbus 0x40016000
    -> nvic@22

egu3 : Miscellaneous.NRF52840_EGU @ sysbus 0x40017000
    -> nvic@23

egu4 : Miscellaneous.NRF52840_EGU @ sysbus 0x40018000
    -> nvic@24

egu5 : Miscellaneous.NRF52840_EGU @ sysbus 0x40019000
    -> nvic@25

sysbus:
    init:
        Tag <0x10000000, 0x10000FFF> "FICR"
        Tag <0x100000a0, 0x100000a3> "DEVICEADDRTYPE" 0x1 # random address
        Tag <0x100000a4, 0x100000a7> "DEVICEADDR[0]" 0xAABBCCDD
        Tag <0x10001200, 0x10001203> "PSELRESET"
)CHUDWARE_TMPL"},
        {"rp2040.repl", R"CHUDWARE_TMPL(// RP2040 (Raspberry Pi Pico) minimal platform description for Renode (MIT)
// License: MIT — see templates/renode_platforms/LICENSE
//
// T-FIN2 HONESTY NOTE: stock Renode ships NO RP2040 SoC model (checked upstream
// renode/renode platforms/ 2026-07-10). The maintained community model
// (github.com/matgla/Renode_RP2040) needs custom C# peripherals compiled into
// Renode, so it cannot be bundled here. This file is therefore a MINIMAL,
// locally-authored platform: one Cortex-M0+ core, ROM/flash/SRAM at the RP2040
// memory map, and both UARTs using Renode's stock ARM PL011 model (the RP2040's
// UART is PL011-derived). Good for bare-metal firmware that drives UART registers
// directly; NOT sufficient for pico-sdk stdio (clocks/resets/SIO are unmodelled).
// platform_db reports rp2040 capabilities accordingly: uart=true (minimal),
// gpio/adc/i2c/spi/timers=false. [~] live-verify: renode absent on this machine.

nvic: IRQControllers.NVIC @ sysbus 0xE000E000
    priorityMask: 0xF0
    systickFrequency: 125000000
    IRQ -> cpu@0

cpu: CPU.CortexM @ sysbus
    cpuType: "cortex-m0+"
    nvic: nvic

rom: Memory.MappedMemory @ sysbus 0x00000000
    size: 0x4000

flash: Memory.MappedMemory @ sysbus 0x10000000
    size: 0x200000

sram: Memory.MappedMemory @ sysbus 0x20000000
    size: 0x42000

uart0: UART.PL011 @ sysbus 0x40034000
    -> nvic@20

uart1: UART.PL011 @ sysbus 0x40038000
    -> nvic@21
)CHUDWARE_TMPL"},
        {"stm32f103.repl", R"CHUDWARE_TMPL(// STM32F103 platform description for Renode (MIT)
// Source: renode 1.16.1 platforms/cpus/stm32f103.repl (bundled)
// License: MIT — see templates/renode_platforms/LICENSE and upstream renode LICENSE.
//
// Flat (no includes). Offline-safe: ApplySVD network fetch removed from sysbus init.
// Ported for Renode 1.16.1: drop DMA/STM32G0DMA wiring that is not in the bundled
// 1.16.1 F103 platform; keep usart1: inline for emulator console attachment.

fsmcBank1: Memory.MappedMemory @ sysbus 0x60000000
    size: 0x10000000

sram: Memory.MappedMemory @ sysbus 0x20000000
    size: 0x10000000

flash: Memory.MappedMemory @ sysbus 0x00000000
    size: 0x20000000

usart1: UART.STM32_UART @ sysbus <0x40013800, +0x100>
    -> nvic@37

usart2: UART.STM32_UART @ sysbus <0x40004400, +0x100>
    -> nvic@38

usart3: UART.STM32_UART @ sysbus <0x40004800, +0x100>
    -> nvic@39

usart4: UART.STM32_UART @ sysbus <0x40004C00, +0x100>
    -> nvic@52

usart5: UART.STM32_UART @ sysbus <0x40005000, +0x100>
    -> nvic@53

nvic: IRQControllers.NVIC @ sysbus 0xE000E000
    priorityMask: 0xF0
    systickFrequency: 72000000
    IRQ -> cpu@0

cpu: CPU.CortexM @ sysbus
    cpuType: "cortex-m3"
    nvic: nvic

i2c1: I2C.STM32F4_I2C @ sysbus 0x40005400
    EventInterrupt -> nvic@31
    ErrorInterrupt -> nvic@32

i2c2: I2C.STM32F4_I2C @ sysbus 0x40005800
    EventInterrupt -> nvic@33
    ErrorInterrupt -> nvic@34

exti: IRQControllers.STM32F4_EXTI @ sysbus 0x40010400
    numberOfOutputLines: 19
    [0-4] -> nvic@[6-10]
    [5-9] -> nvicInput23@[0-4]
    [10-15] -> nvicInput40@[0-5]

nvicInput23: Miscellaneous.CombinedInput @ none
    numberOfInputs: 5
    -> nvic@23

nvicInput40: Miscellaneous.CombinedInput @ none
    numberOfInputs: 6
    -> nvic@40

gpioPortA: GPIOPort.STM32F1GPIOPort @ sysbus <0x40010800, +0x400>
    invertedAFPins: [[7, 1]]
    [0-15] -> exti@[0-15]

gpioPortB: GPIOPort.STM32F1GPIOPort @ sysbus <0x40010C00, +0x400>
    invertedAFPins: [[13, 1], [14, 1], [15, 1], [0, 1], [1, 1]]
    [0-15] -> exti@[0-15]

gpioPortC: GPIOPort.STM32F1GPIOPort @ sysbus <0x40011000, +0x400>
    [0-15] -> exti@[0-15]

gpioPortD: GPIOPort.STM32F1GPIOPort @ sysbus <0x40011400, +0x400>
    [0-15] -> exti@[0-15]

gpioPortE: GPIOPort.STM32F1GPIOPort @ sysbus <0x40011800, +0x400>
    invertedAFPins: [[8, 1], [10, 1], [12, 1]]
    [0-15] -> exti@[0-15]

gpioPortF: GPIOPort.STM32F1GPIOPort @ sysbus <0x40011C00, +0x400>
    [0-15] -> exti@[0-15]

gpioPortG: GPIOPort.STM32F1GPIOPort @ sysbus <0x40012000, +0x400>
    [0-15] -> exti@[0-15]

timer1: Timers.STM32_Timer @ sysbus 0x40012c00
    BreakInterrupt -> nvic@24
    UpdateInterrupt -> nvic@25
    [TriggerInterrupt, CommutationInterrupt] -> nvic@[26, 26]
    CaptureCompareInterrupt -> nvic@27
    frequency: 10000000
    initialLimit: 0xFFFF

timer2: Timers.STM32_Timer @ sysbus 0x40000000
    -> nvic@28
    frequency: 10000000
    initialLimit: 0xFFFF

timer3: Timers.STM32_Timer @ sysbus 0x40000400
    -> nvic@29
    frequency: 10000000
    initialLimit: 0xFFFF

timer4: Timers.STM32_Timer @ sysbus 0x40000800
    -> nvic@30
    frequency: 10000000
    initialLimit: 0xFFFF

timer5: Timers.STM32_Timer @ sysbus 0x40000C00
    -> nvic@50
    frequency: 10000000
    initialLimit: 0xFFFF

timer6: Timers.STM32_Timer @ sysbus 0x40001000
    -> nvic@54
    frequency: 10000000
    initialLimit: 0xFFFF

timer7: Timers.STM32_Timer @ sysbus 0x40001400
    -> nvic@55
    frequency: 10000000
    initialLimit: 0xFFFF

timer8: Timers.STM32_Timer @ sysbus 0x40013400
    BreakInterrupt -> nvic@43
    UpdateInterrupt -> nvic@44
    [TriggerInterrupt, CommutationInterrupt] -> nvic@[45, 45]
    CaptureCompareInterrupt -> nvic@46
    frequency: 10000000
    initialLimit: 0xFFFF

timer9: Timers.STM32_Timer @ sysbus 0x40014c00
    -> nvic@24
    frequency: 10000000
    initialLimit: 0xFFFF

timer10: Timers.STM32_Timer @ sysbus 0x40015000
    -> nvic@25
    frequency: 10000000
    initialLimit: 0xFFFF

timer11: Timers.STM32_Timer @ sysbus 0x40015400
    -> nvic@26
    frequency: 10000000
    initialLimit: 0xFFFF

timer12: Timers.STM32_Timer @ sysbus 0x40001800
    -> nvic@43
    frequency: 10000000
    initialLimit: 0xFFFF

timer13: Timers.STM32_Timer @ sysbus 0x40001C00
    -> nvic@44
    frequency: 10000000
    initialLimit: 0xFFFF

timer14: Timers.STM32_Timer @ sysbus 0x40002000
    -> nvic@45
    frequency: 10000000
    initialLimit: 0xFFFF

afio: GPIOPort.STM32F1AFIO @ sysbus 0x40010000
    gpioPorts: [gpioPortA, gpioPortB, gpioPortC, gpioPortD, gpioPortE, gpioPortF, gpioPortG]

// Alternate function connections
// The syntax is:
//     gpioPort#pinNumber@timerNumber
// Meaning `1 -> gpioPortA#2@5` connects output 1 to pin 2 of GPIO port A and it receives its signal from timer 5
timer1:
    0 -> gpioPortA#08@01 | gpioPortE#09@01 | gpioPortB#13@01 | gpioPortA#07@01 | gpioPortE#08@01
    1 -> gpioPortA#09@01 | gpioPortE#11@01 | gpioPortB#14@01 | gpioPortB#00@01 | gpioPortE#10@01
    2 -> gpioPortA#10@01 | gpioPortE#13@01 | gpioPortB#15@01 | gpioPortB#01@01 | gpioPortE#12@01
    3 -> gpioPortA#11@01 | gpioPortE#14@01

timer2:
    0 -> gpioPortA#00@02 | gpioPortA#15@02
    1 -> gpioPortA#01@02 | gpioPortB#03@02
    2 -> gpioPortA#02@02 | gpioPortB#10@02
    3 -> gpioPortA#03@02 | gpioPortB#11@02

timer3:
    0 -> gpioPortA#06@03 | gpioPortB#04@03 | gpioPortC#06@03
    1 -> gpioPortA#07@03 | gpioPortB#05@03 | gpioPortC#07@03
    2 -> gpioPortB#00@03 | gpioPortC#08@03
    3 -> gpioPortB#01@03 | gpioPortC#09@03

timer4:
    0 -> gpioPortB#06@04 | gpioPortD#12@04
    1 -> gpioPortB#07@04 | gpioPortD#13@04
    2 -> gpioPortB#08@04 | gpioPortD#14@04
    3 -> gpioPortB#09@04 | gpioPortD#15@04

timer5:
    3 -> gpioPortA#03@05

timer9:
    0 -> gpioPortA#02@09 | gpioPortE#05@09
    1 -> gpioPortA#03@09 | gpioPortE#06@09

timer10:
    0 -> gpioPortB#08@10 | gpioPortF#06@10

timer11:
    0 -> gpioPortB#09@11 | gpioPortF#07@11

timer13:
    0 -> gpioPortA#06@13 | gpioPortF#08@13

timer14:
    0 -> gpioPortA#07@14 | gpioPortF#09@14

sysbus:
    init:
        Tag <0x40021000, 0x40021003> "RCC_CR" 0x0A020083
)CHUDWARE_TMPL"},
        {"stm32f4_discovery.repl", R"CHUDWARE_TMPL(// STM32F4 Discovery platform description for Renode (MIT)
// Source: based on Renode's STM32F4 platform files (https://github.com/renode/renode-infrastructure)
// License: MIT — see templates/renode_platforms/LICENSE
//
// Flat (no `using "cpus/..."` includes) so engine_oracle/firmware/emulator.py can parse
// the platform TEXT (greps `usart1:`) and use gpio_regmap ODR snapshots.
// Ported for Renode 1.16.1: GPIOPort.STM32F4GPIOPort -> GPIOPort.STM32_GPIOPort
// (plus outputSpeedResetValue) matching bundled platforms/cpus/stm32f4.repl.

nvic: IRQControllers.NVIC @ sysbus 0xE000E000
    priorityMask: 0xF0
    systickFrequency: 16000000
    IRQ -> cpu@0

cpu: CPU.CortexM @ sysbus
    cpuType: "cortex-m4f"
    nvic: nvic

usart1: Chudware.TimedSTM32UART @ sysbus <0x40011000, +0x100>
    name: "usart1"
    frequency: 16000000
    -> nvic@37

flash: Memory.MappedMemory @ sysbus 0x08000000
    size: 0x100000

sram: Memory.MappedMemory @ sysbus 0x20000000
    size: 0x20000

// GPIO ports A–E so pin-level assertions have a peripheral to observe.
// Model + register map from Renode 1.16.1 platforms/cpus/stm32f4.repl (MIT):
// GPIOPort.STM32_GPIOPort at 0x40020000 + 0x400*port, mode/PUPD/speed reset values per RM0090.
gpioPortA: GPIOPort.STM32_GPIOPort @ sysbus <0x40020000, +0x400>
    modeResetValue: 0xA8000000
    outputSpeedResetValue: 0x0C000000
    pullUpPullDownResetValue: 0x64000000
    numberOfAFs: 16

gpioPortB: GPIOPort.STM32_GPIOPort @ sysbus <0x40020400, +0x400>
    modeResetValue: 0x00000280
    outputSpeedResetValue: 0x000000C0
    pullUpPullDownResetValue: 0x00000100
    numberOfAFs: 16

gpioPortC: GPIOPort.STM32_GPIOPort @ sysbus <0x40020800, +0x400>
    numberOfAFs: 16

gpioPortD: GPIOPort.STM32_GPIOPort @ sysbus <0x40020C00, +0x400>
    numberOfAFs: 16

gpioPortE: GPIOPort.STM32_GPIOPort @ sysbus <0x40021000, +0x400>
    numberOfAFs: 16

// Peripheral pack from Renode 1.16.1 platforms/cpus/stm32f4.repl (MIT).
// Local changes vs upstream: DMA request links dropped (this curated platform declares
// no DMA controller) and the rcc->timer9 reset link dropped (no RCC model here).

usart2: Chudware.TimedSTM32UART @ sysbus <0x40004400, +0x100>
    name: "usart2"
    frequency: 16000000
    -> nvic@38

spi1: Chudware.TimedSTM32SPI @ sysbus 0x40013000
    name: "spi1"
    frequency: 16000000
    IRQ->nvic@35

spi2: Chudware.TimedSTM32SPI @ sysbus 0x40003800
    name: "spi2"
    frequency: 16000000
    IRQ->nvic@36

spi3: Chudware.TimedSTM32SPI @ sysbus 0x40003C00
    name: "spi3"
    frequency: 16000000
    IRQ->nvic@51

i2c1: Chudware.TimedSTM32I2C @ sysbus 0x40005400
    name: "i2c1"
    frequency: 16000000
    EventInterrupt -> nvic@31
    ErrorInterrupt -> nvic@32

i2c2: Chudware.TimedSTM32I2C @ sysbus 0x40005800
    name: "i2c2"
    frequency: 16000000
    EventInterrupt -> nvic@33
    ErrorInterrupt -> nvic@34

i2c3: Chudware.TimedSTM32I2C @ sysbus 0x40005C00
    name: "i2c3"
    frequency: 16000000
    EventInterrupt -> nvic@72
    ErrorInterrupt -> nvic@73

can1: Chudware.TimedSTM32CAN @ sysbus 0x40006400
    name: "can1"
    frequency: 16000000
    [0, 1, 2, 3] -> nvic@[19, 20, 21, 22]

can2: Chudware.TimedSTM32CAN @ sysbus 0x40006800
    name: "can2"
    frequency: 16000000
    master: can1
    [0, 1, 2, 3] -> nvic@[63, 64, 65, 66]

timer1: Timers.STM32_Timer @ sysbus 0x40010000
    BreakInterrupt -> nvic@24
    UpdateInterrupt -> nvic@25
    [TriggerInterrupt, CommutationInterrupt] -> nvic@[26, 26]
    CaptureCompareInterrupt -> nvic@27
    frequency: 10000000
    initialLimit: 0xFFFF

timer2: Timers.STM32_Timer @ sysbus 0x40000000
    -> nvic@28
    frequency: 10000000
    initialLimit: 0xFFFFFFFF

timer3: Timers.STM32_Timer @ sysbus 0x40000400
    -> nvic@29
    frequency: 10000000
    initialLimit: 0xFFFF

timer4: Timers.STM32_Timer @ sysbus 0x40000800
    -> nvic@30
    frequency: 10000000
    initialLimit: 0xFFFF

timer5: Timers.STM32_Timer @ sysbus 0x40000C00
    -> nvic@50
    frequency: 10000000
    initialLimit: 0xFFFFFFFF
)CHUDWARE_TMPL"},
    };
    return kFiles;
}

}  // namespace chudware::firmware
