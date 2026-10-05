/* Bare-metal SPI consumer fixture; link with stm32_hello.ld.
 * Scenario: SPI1 clock 20MHz, peer 8-bit mode SPI_MODE, register map
 * 10:A5,11:5A, read_mask=80,address_mask=7F, auto_increment=true.
 * Wire gpioPortA output 4 -> spi1 input 0. Healthy timing prints SPI_GOOD;
 * unchanged ELF with unsuitable peer mode/CS/MISO timing prints SPI_BAD.
 * SPI_OVR_GOOD proves finite DR capacity and DR->SR overrun clearing,
 * checking the cleared state on a subsequent SR read rather than the clearing read.
 * SPI_MODF_GOOD proves actual internal-NSS mode fault and SR->CR1 clearing.
 * Compile SPI_MODE=0..3 and SPI_LSB_FIRST=0/1 for mode/order coverage.
 */
#include <stdint.h>
#ifndef SPI_MODE
#define SPI_MODE 0
#endif
#ifndef SPI_LSB_FIRST
#define SPI_LSB_FIRST 0
#endif
#define MMIO(a) (*(volatile uint32_t *)(a))
#define RCC_AHB1ENR MMIO(0x40023830u)
#define RCC_APB2ENR MMIO(0x40023844u)
#define GPIOA_MODER MMIO(0x40020000u)
#define GPIOA_BSRR MMIO(0x40020018u)
#define SPI_CR1 MMIO(0x40013000u)
#define SPI_SR MMIO(0x40013008u)
#define SPI_DR (*(volatile uint8_t *)0x4001300Cu)
#define UART_SR MMIO(0x40011000u)
#define UART_DR MMIO(0x40011004u)
#define UART_BRR MMIO(0x40011008u)
#define UART_CR1 MMIO(0x4001100Cu)
#define SPI_CONTROL (0x344u | ((SPI_MODE & 1u) << 0) | ((SPI_MODE & 2u) << 0) | (SPI_LSB_FIRST << 7))
extern uint32_t _estack;
void Reset_Handler(void);
void Default_Handler(void);
__attribute__((section(".isr_vector")))
void (*const g_pfnVectors[])(void) = {
    (void (*)(void))(&_estack), Reset_Handler, Default_Handler,
    Default_Handler, Default_Handler, Default_Handler, Default_Handler
};
static int wait_flag(uint32_t flag, int wanted)
{
    for(uint32_t i = 0; i < 100000u; ++i)
        if(((SPI_SR & flag) != 0) == wanted) return 1;
    return 0;
}
static void puts_uart(const char *s)
{
    while(*s) {
        while(!(UART_SR & 0x80u)) { }
        UART_DR = (uint8_t)*s++;
    }
    while(!(UART_SR & 0x40u)) { }
}
static int transfer(uint8_t tx, uint8_t *rx)
{
    if(!wait_flag(2u, 1)) return 0;
    SPI_DR = tx;
    if(!wait_flag(1u, 1)) return 0;
    *rx = SPI_DR;
    return wait_flag(0x80u, 0);
}
static void select_peer(void)
{
    GPIOA_BSRR = 1u << 20;
    for(volatile unsigned i = 0; i < 32; ++i) { }
}
static void deselect_peer(void)
{
    for(volatile unsigned i = 0; i < 32; ++i) { }
    GPIOA_BSRR = 1u << 4;
}
void Reset_Handler(void)
{
    RCC_AHB1ENR |= 1u;
    RCC_APB2ENR |= (1u << 12) | (1u << 4);
    GPIOA_MODER = (GPIOA_MODER & ~(3u << 8)) | (1u << 8);
    GPIOA_BSRR = 1u << 4;
    UART_BRR = 0x2D9u;
    UART_CR1 = 0x2008u;
    SPI_CR1 = SPI_CONTROL;
    uint8_t ignored = 0, high = 0, low = 0;
    select_peer();
    int good = transfer(0x90u, &ignored) && transfer(0, &high) && transfer(0, &low);
    deselect_peer();
    puts_uart(good && high == 0xA5u && low == 0x5Au ? "SPI_GOOD\r\n" : "SPI_BAD\r\n");
    select_peer();
    SPI_DR = 0x90u;
    int first = wait_flag(1u, 1) && wait_flag(0x80u, 0);
    SPI_DR = 0;
    int second = wait_flag(0x80u, 0);
    uint32_t status = SPI_SR;
    ignored = SPI_DR; /* Hardware clear order: DR first, then SR. */
    (void)SPI_SR; /* Complete read-to-clear; this read may return the old OVR. */
    uint32_t cleared = SPI_SR;
    deselect_peer();
    puts_uart(first && second && (status & 0x40u) && !(cleared & 0x40u) ? "SPI_OVR_GOOD\r\n" : "SPI_OVR_BAD\r\n");
    SPI_CR1 = SPI_CONTROL & ~0x100u; /* SSM=1, SSI=0 while master enabled. */
    status = SPI_SR;
    uint32_t fault_control = SPI_CR1;
    SPI_CR1 = SPI_CONTROL;
    puts_uart((status & 0x20u) && !(fault_control & 0x44u) && !(SPI_SR & 0x20u) ? "SPI_MODF_GOOD\r\n" : "SPI_MODF_BAD\r\n");
    (void)ignored;
    for(;;) { }
}
void Default_Handler(void) { for(;;) { } }
