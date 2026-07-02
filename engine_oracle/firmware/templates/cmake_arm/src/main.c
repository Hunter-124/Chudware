/* Bare-metal blink skeleton — AI-CAD firmware template (cmake_arm baseline).
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
