/* Semihosting hello-and-exit test fixture — a minimal bare-metal
 * Cortex-M image that prints two lines over the ARM semihosting console and
 * exits cleanly via SYS_EXIT, so the QEMU leg of fw_sim_run can be exercised
 * LIVE in tests (qemu-system-arm -M mps2-an385 -semihosting) without relying
 * on the looping template main. Compiled in-test with arm-none-eabi-gcc when
 * present; the test SKIPs honestly otherwise. No libc, no startup code beyond
 * the vector table below.
 *
 * Lines printed (both machine-parseable by the emulator's parsers):
 *   CHUDWARE_GPIO_EVT PA5=1 t=10ms   (a stamped gpio event, firmware-side shim idiom)
 *   hello world                   (the uart: assertion target)
 */
#include <stdint.h>

/* ARM semihosting: bkpt #0xAB with r0 = reason, r1 = arg. */
static inline int semihost_call(int reason, const void *arg)
{
    register int r0 __asm__("r0") = reason;
    register const void *r1 __asm__("r1") = arg;
    __asm__ volatile("bkpt #0xAB" : "+r"(r0) : "r"(r1) : "memory");
    return r0;
}

#define SYS_WRITE0 0x04
#define SYS_EXIT   0x18
#define ADP_STOPPED_APPLICATION_EXIT 0x20026

extern uint32_t _estack;

void Reset_Handler(void);
void Default_Handler(void);

__attribute__((section(".isr_vector")))
void (* const g_pfnVectors[])(void) = {
    (void (*)(void))(&_estack),
    Reset_Handler,
    Default_Handler,
    Default_Handler,
    Default_Handler,
    Default_Handler,
    Default_Handler,
};

void Reset_Handler(void)
{
    semihost_call(SYS_WRITE0, "CHUDWARE_GPIO_EVT PA5=1 t=10ms\n");
    semihost_call(SYS_WRITE0, "hello world\n");
    semihost_call(SYS_EXIT, (const void *)(uintptr_t)ADP_STOPPED_APPLICATION_EXIT);
    for (;;) {
    }
}

void Default_Handler(void)
{
    for (;;) {
    }
}
