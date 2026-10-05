/* Bare-metal consumer probe for TimedSTM32UART; no peripheral-model internals.
 * Compile for cortex-m4/thumb and link with uart_f4_probe.ld. Input USART2 9600 8N1;
 * USART1 prints a paced console banner at BRR=0x8B, both clocks explicitly 16MHz.
 * With uart_f4_probe.ld, uart_probe is at 0x20000000; control is 0x20000004.
 * Write control=2, wait for control_applied=2 (0x20000320), then send at least
 * two frames without draining DR to produce ORE. undrained_status (0x20000324)
 * samples actual SR without reading DR. Write control=3 for SR->DR recovery;
 * control_applied returns to 1 when IRQ draining resumes.
 * SRAM uint32_t fields: count +0x08, errors +0x0c, recovery_count +0x10,
 * recovery_status/value/after +0x14/+0x18/+0x1c; 64-entry value/before/after
 * arrays at +0x20/+0x120/+0x220. Entries pair each DR read with actual SR
 * before/after; count may exceed capacity, but only the first 64 are retained.
 * UART_PROBE_PARITY=1 selects 8E1; =2 selects 8O1 (M=1, parity occupies ninth bit).
 * UART_PROBE_ONEBIT=1 selects CR3.ONEBIT; default 0 retains noise detection.
 */
#include <stdint.h>

#ifndef UART_PROBE_BRR
#define UART_PROBE_BRR 0x683u
#endif
#ifndef UART_PROBE_PARITY
#define UART_PROBE_PARITY 0
#endif
#ifndef UART_PROBE_ONEBIT
#define UART_PROBE_ONEBIT 0
#endif

#define REG32(address) (*(volatile uint32_t *)(address))
#define SR(base) REG32((base) + 0x00u)
#define DR(base) REG32((base) + 0x04u)
#define BRR(base) REG32((base) + 0x08u)
#define CR1(base) REG32((base) + 0x0cu)
#define CR3(base) REG32((base) + 0x14u)
#define UART1 0x40011000u
#define UART2 0x40004400u
#define RXNE (1u << 5)
#define ERRORS 15u
#define NVIC_ISER1 REG32(0xe000e104u)
#define NVIC_ICER1 REG32(0xe000e184u)
#define UART2_IRQ_MASK (1u << 6)
#define RECORDS 64u

struct UartProbe
{
    uint32_t magic;
    uint32_t control;
    uint32_t count;
    uint32_t errors;
    uint32_t recovery_count;
    uint32_t recovery_status;
    uint32_t recovery_value;
    uint32_t recovery_after;
    uint32_t value[RECORDS];
    uint32_t before[RECORDS];
    uint32_t after[RECORDS];
    uint32_t control_applied;
    uint32_t undrained_status;
};
volatile struct UartProbe uart_probe;

static void uart2_handler(void)
{
    uint32_t before = SR(UART2);
    if((before & (RXNE | ERRORS)) == 0u) return;
    uint32_t value = DR(UART2);
    uint32_t after = SR(UART2);
    uint32_t index = uart_probe.count;
    uart_probe.errors |= before & ERRORS;
    if(index < RECORDS)
    {
        uart_probe.value[index] = value;
        uart_probe.before[index] = before;
        uart_probe.after[index] = after;
    }
    uart_probe.count = index + 1u;
}

extern uint32_t __bss_start, __bss_end;
static void reset_handler(void)
{
    for(uint32_t *p = &__bss_start; p < &__bss_end; ++p) *p = 0u;
    uart_probe.control = 1u;
    uart_probe.control_applied = 1u;
    BRR(UART2) = UART_PROBE_BRR;
    uint32_t receive_control = (1u << 13) | (1u << 2) | (1u << 5) | (1u << 8);
#if UART_PROBE_PARITY != 0
    receive_control |= (1u << 12) | (1u << 10);
#if UART_PROBE_PARITY == 2
    receive_control |= (1u << 9);
#endif
#endif
    CR1(UART2) = receive_control;
    CR3(UART2) = 1u | (UART_PROBE_ONEBIT ? (1u << 11) : 0u);
    /* EIE: FE/NF/ORE, plus PEIE/RXNEIE in CR1; ONEBIT disables NF voting. */
    NVIC_ISER1 = UART2_IRQ_MASK;
    BRR(UART1) = 0x8bu;
    CR1(UART1) = (1u << 13) | (1u << 3);
    const char *banner = "UART_PROBE_READY\r\n";
    while(*banner)
    {
        while((SR(UART1) & (1u << 7)) == 0u) {}
        DR(UART1) = (uint8_t)*banner++;
    }
    uart_probe.magic = 0x55525434u;
    for(;;)
    {
        uint32_t command = uart_probe.control;
        if(command == 2u)
        {
            NVIC_ICER1 = UART2_IRQ_MASK;
            uart_probe.undrained_status = SR(UART2);
            uart_probe.control_applied = 2u;
        }
        else if(command == 3u)
        {
            NVIC_ICER1 = UART2_IRQ_MASK;
            uart_probe.recovery_status = SR(UART2);
            uart_probe.recovery_value = DR(UART2);
            uart_probe.recovery_after = SR(UART2);
            uart_probe.errors |= uart_probe.recovery_status & ERRORS;
            uart_probe.recovery_count++;
            uart_probe.control = 1u;
            NVIC_ISER1 = UART2_IRQ_MASK;
            uart_probe.control_applied = 1u;
        }
        else if(command == 1u)
        {
            NVIC_ISER1 = UART2_IRQ_MASK;
            uart_probe.control_applied = 1u;
        }
    }
}

__attribute__((section(".vectors"), used))
const uintptr_t vectors[55] =
{
    [0] = 0x20020000u,
    [1] = (uintptr_t)reset_handler,
    [54] = (uintptr_t)uart2_handler
};
