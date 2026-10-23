/* Renode live-test fixture — bare-metal STM32F4 image that writes
 * "hello renode" to USART1 and drives PA5 high, so the Renode leg of
 * fw_sim_run can be exercised LIVE in tests (stm32f4_discovery.repl declares
 * usart1 + gpioPortA). Compiled in-test with arm-none-eabi-gcc when present;
 * the test SKIPs honestly otherwise. No libc.
 *
 * Register footprint (STM32F407):
 *   RCC    0x40023800: AHB1ENR (+0x30) bit0 GPIOAEN, APB2ENR (+0x44) bit4 USART1EN
 *   GPIOA  0x40020000: MODER +0x00, ODR +0x14
 *   USART1 0x40011000: SR +0x00, DR +0x04, BRR +0x08, CR1 +0x0C
 */
#include <stdint.h>

#define RCC_AHB1ENR  (*(volatile uint32_t *)0x40023830u)
#define RCC_APB2ENR  (*(volatile uint32_t *)0x40023844u)
#define GPIOA_MODER  (*(volatile uint32_t *)0x40020000u)
#define GPIOA_ODR    (*(volatile uint32_t *)0x40020014u)
#define USART1_SR    (*(volatile uint32_t *)0x40011000u)
#define USART1_DR    (*(volatile uint32_t *)0x40011004u)
#define USART1_BRR   (*(volatile uint32_t *)0x40011008u)
#define USART1_CR1   (*(volatile uint32_t *)0x4001100Cu)

#define USART_CR1_UE (1u << 13)
#define USART_CR1_TE (1u << 3)
#define USART_SR_TC  (1u << 6)

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

static void uart_puts(const char *s)
{
    while (*s) {
        USART1_DR = (uint32_t)(uint8_t)*s++;
        while (!(USART1_SR & USART_SR_TC)) {
        }
    }
}

void Reset_Handler(void)
{
    RCC_AHB1ENR |= 1u;        /* GPIOAEN */
    RCC_APB2ENR |= (1u << 4); /* USART1EN */

    /* PA5 general-purpose output. */
    GPIOA_MODER = (GPIOA_MODER & ~(3u << (5u * 2u))) | (1u << (5u * 2u));

    USART1_BRR = 0x2D9u;  /* ~115200 @ 48 MHz (16x oversampling) */
    USART1_CR1 = USART_CR1_UE | USART_CR1_TE;
    uart_puts("hello renode\r\n");

    GPIOA_ODR |= (1u << 5);  /* drive PA5 high */

    for (;;) {
    }
}

void Default_Handler(void)
{
    for (;;) {
    }
}
