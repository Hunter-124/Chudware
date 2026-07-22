/* Linker script for the stm32_hello.c Renode fixture — STM32F407 memory map
 * (flash at 0x08000000, SRAM at 0x20000000). */
OUTPUT_FORMAT("elf32-littlearm")
ENTRY(Reset_Handler)

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 512K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);

SECTIONS
{
    .text : {
        KEEP(*(.isr_vector))
        *(.text*)
        *(.rodata*)
    } > FLASH
    .data : { *(.data*) } > RAM
    .bss  : { *(.bss*) *(COMMON) } > RAM
}
