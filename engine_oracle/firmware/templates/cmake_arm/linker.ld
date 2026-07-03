/* Minimal Cortex-M linker script — AI-CAD firmware template (cmake_arm baseline).
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
