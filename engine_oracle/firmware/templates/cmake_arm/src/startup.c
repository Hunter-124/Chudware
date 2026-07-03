/* Minimal reset/vector table — AI-CAD firmware template (cmake_arm baseline).
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
