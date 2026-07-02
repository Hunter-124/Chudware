/* Minimal reset/vector stub — AI-CAD firmware template (cmake_arm baseline).
 *
 * A real target supplies a full vector table + startup from its CMSIS device pack; this stub keeps
 * the template link-complete (Reset_Handler -> main) so a configure/compile succeeds when a real
 * arm-none-eabi toolchain + linker script are present. Replaced per MCU by the agent.
 */
#include <stdint.h>

extern int main(void);

void Reset_Handler(void)
{
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
