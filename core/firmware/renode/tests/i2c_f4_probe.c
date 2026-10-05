/* STM32F4 I2C1 master acceptance fixture; freestanding Cortex-M4/Thumb.
 * HSI/APB1/APB2 = 16MHz; USART1 BRR=0x8B. PB6/PB7 use AF4 open drain
 * with NO internal pullups: the scenario owns the external electrical network.
 * Start from true F4 reset clocks: 16MHz HSI, undivided AHB/APB1/APB2.
 * The acceptance platform supplies these clocks explicitly but has no RCC
 * readiness model. Do not poll unmapped HSIRDY or reconfigure SYSCLK; RCC
 * peripheral clock-enable/reset writes below remain valid on real hardware.
 * One ELF starts at CCR=80 (100kHz); a real failure requests STOP, clears error
 * flags, resets the controller and retries at CCR=800 (10kHz, 50us low time).
 * A 30us peer response therefore misses the fast ACK sample but fits the retry.
 * No monitor commands, synthetic failures, host sleeps or sensor FIFO are used.
 * I2C_PROBE_STOP_ONLY_RECOVERY=1 first exercises STOP-only recovery without a
 * PE transition or SWRST. CCR stays 80 (F4 permits CCR writes only with PE=0):
 * after at least 100us virtual settling, a real address retry must still NACK
 * with a persistent 30us peer delay, not encounter a stale peer-driven SDA.
 * The subsequent ordinary reset/CCR800 retry still checks ID and burst bytes.
 *
 * Declare MPU6050 at 0x68 and a stable stream with registers 0x3B..0x48:
 *   01 00 FE 00 40 00 05 50 00 80 FF C0 00 20
 * WHO_AM_I remains the real model's 0x68. The fixture wakes PWR_MGMT_1 before
 * checking all fourteen bytes in one repeated-START burst/snapshot, then checks
 * 0x3B/0x3C again with the distinct F4 two-byte POS receive sequence. The stream
 * must be installed before execution and remain unchanged during this probe.
 * I2C_PROBE_ACK_AFTER_ADDR=1 exercises ST's documented POS=1 before ADDR clear,
 * ACK=0 after ADDR clear ordering; default 0 clears ACK before ADDR instead.
 *
 * SysTick advances software deadlines every 100us. Each pending flag/STOP gets
 * at most 2ms; the overall transaction window closes at 30ms. Final diagnostics
 * may finish just after that window, then the CPU idles permanently.
 */
#include <stdint.h>

#ifndef I2C_PROBE_STOP_ONLY_RECOVERY
#define I2C_PROBE_STOP_ONLY_RECOVERY 0
#endif

#ifndef I2C_PROBE_ACK_AFTER_ADDR
#define I2C_PROBE_ACK_AFTER_ADDR 0
#endif
#if I2C_PROBE_ACK_AFTER_ADDR != 0 && I2C_PROBE_ACK_AFTER_ADDR != 1
#error "I2C_PROBE_ACK_AFTER_ADDR must be 0 or 1"
#endif

#define REG32(address) (*(volatile uint32_t *)(address))
#define RCC_AHB1ENR REG32(0x40023830u)
#define RCC_APB1RSTR REG32(0x40023820u)
#define RCC_APB1ENR REG32(0x40023840u)
#define RCC_APB2ENR REG32(0x40023844u)
#define GPIOA 0x40020000u
#define GPIOB 0x40020400u
#define MODER(base) REG32((base) + 0x00u)
#define OTYPER(base) REG32((base) + 0x04u)
#define OSPEEDR(base) REG32((base) + 0x08u)
#define PUPDR(base) REG32((base) + 0x0cu)
#define AFRL(base) REG32((base) + 0x20u)
#define AFRH(base) REG32((base) + 0x24u)
#define UART_SR REG32(0x40011000u)
#define UART_DR REG32(0x40011004u)
#define UART_BRR REG32(0x40011008u)
#define UART_CR1 REG32(0x4001100cu)
#define I2C_CR1 REG32(0x40005400u)
#define I2C_CR2 REG32(0x40005404u)
#define I2C_OAR1 REG32(0x40005408u)
#define I2C_DR REG32(0x40005410u)
#define I2C_SR1 REG32(0x40005414u)
#define I2C_SR2 REG32(0x40005418u)
#define I2C_CCR REG32(0x4000541cu)
#define I2C_TRISE REG32(0x40005420u)
#define SYST_CSR REG32(0xe000e010u)
#define SYST_RVR REG32(0xe000e014u)
#define SYST_CVR REG32(0xe000e018u)
#define SCB_VTOR REG32(0xe000ed08u)
#define PE 1u
#define START (1u << 8)
#define STOP (1u << 9)
#define ACK (1u << 10)
#define POS (1u << 11)
#define SWRST (1u << 15)
#define SB 1u
#define ADDR 2u
#define BTF 4u
#define RXNE (1u << 6)
#define TXE (1u << 7)
#define BERR (1u << 8)
#define ARLO (1u << 9)
#define AF (1u << 10)
#define ERRORS 0xdf00u
#define BUSY 2u
#define STAGE_TICKS 20u
#define END_TICKS 300u

enum Fault
{
    OK, TIMEOUT_START, TIMEOUT_ADDRESS, TIMEOUT_TX, TIMEOUT_RX, TIMEOUT_STOP,
    NACK_ADDRESS, NACK_DATA, BUS_ERROR, ARBITRATION_LOST, OTHER_ERROR,
    BAD_ID, BAD_SAMPLE
};

struct I2cProbe
{
    uint32_t magic;
    uint32_t ticks;
    uint32_t attempt;
    uint32_t ccr;
    uint32_t first_fault;
    uint32_t retry_fault;
    uint32_t fault_sr1;
    uint32_t fault_sr2;
    uint32_t stop_timeout;
    uint32_t sr1_after_clear;
    uint32_t sr1_after_reset;
    uint32_t sr2_after_reset;
    uint32_t id;
    uint32_t sample_count;
    uint32_t sample[14];
    uint32_t passed;
    uint32_t completed;
    uint32_t stop_only_fault;
    uint32_t stop_only_completed;
    uint32_t short_sample_count;
    uint32_t short_sample[2];
};
volatile struct I2cProbe i2c_probe;

static const uint8_t expected_sample[14] =
{
    0x01, 0x00, 0xfe, 0x00, 0x40, 0x00, 0x05, 0x50,
    0x00, 0x80, 0xff, 0xc0, 0x00, 0x20
};

static void systick_handler(void) { ++i2c_probe.ticks; }
static void idle(void) { for(;;) __asm volatile("wfi"); }

static uint32_t deadline(void)
{
    uint32_t end = i2c_probe.ticks + STAGE_TICKS;
    return end < END_TICKS ? end : END_TICKS;
}

static void console(const char *text)
{
    while(*text)
    {
        uint32_t end = i2c_probe.ticks + 10u;
        while((UART_SR & TXE) == 0u)
            if(i2c_probe.ticks >= end) return;
        UART_DR = (uint8_t)*text++;
    }
}

static void report(enum Fault fault)
{
    switch(fault)
    {
    case TIMEOUT_START: console("I2C_TIMEOUT_START\r\n"); break;
    case TIMEOUT_ADDRESS: console("I2C_TIMEOUT_ADDRESS\r\n"); break;
    case TIMEOUT_TX: console("I2C_TIMEOUT_TX\r\n"); break;
    case TIMEOUT_RX: console("I2C_TIMEOUT_RX\r\n"); break;
    case TIMEOUT_STOP: console("I2C_TIMEOUT_STOP\r\n"); break;
    case NACK_ADDRESS: console("I2C_NACK_ADDRESS\r\n"); break;
    case NACK_DATA: console("I2C_NACK_DATA\r\n"); break;
    case BUS_ERROR: console("I2C_BERR\r\n"); break;
    case ARBITRATION_LOST: console("I2C_ARLO\r\n"); break;
    case OTHER_ERROR: console("I2C_HW_ERROR\r\n"); break;
    case BAD_ID: console("I2C_BAD_ID\r\n"); break;
    case BAD_SAMPLE: console("I2C_BAD_SAMPLE\r\n"); break;
    default: break;
    }
}

static enum Fault wait_flag(uint32_t flag, enum Fault timeout, int address)
{
    uint32_t end = deadline();
    for(;;)
    {
        uint32_t status = I2C_SR1;
        if(status & AF) return address ? NACK_ADDRESS : NACK_DATA;
        if(status & BERR) return BUS_ERROR;
        if(status & ARLO) return ARBITRATION_LOST;
        if(status & ERRORS) return OTHER_ERROR;
        if(status & flag) return OK;
        if(i2c_probe.ticks >= end) return timeout;
    }
}

static void clear_address(void)
{
    (void)I2C_SR1;
    (void)I2C_SR2; /* F4 ADDR is cleared ONLY by SR1 followed by SR2. */
}

static enum Fault wait_stop(void)
{
    uint32_t end = deadline();
    while((I2C_SR2 & BUSY) != 0u || (I2C_CR1 & STOP) != 0u)
        if(i2c_probe.ticks >= end) return TIMEOUT_STOP;
    return OK;
}

static enum Fault address(uint8_t value)
{
    I2C_CR1 |= START;
    enum Fault fault = wait_flag(SB, TIMEOUT_START, 1);
    if(fault != OK) return fault;
    I2C_DR = value; /* SR1(SB) -> DR is the actual F4 START clear sequence. */
    return wait_flag(ADDR, TIMEOUT_ADDRESS, 1);
}

static enum Fault send(uint8_t value)
{
    enum Fault fault = wait_flag(TXE, TIMEOUT_TX, 0);
    if(fault != OK) return fault;
    I2C_DR = value;
    return wait_flag(BTF, TIMEOUT_TX, 0);
}

static enum Fault read_registers(uint8_t reg, uint32_t count)
{
    I2C_CR1 = PE | ACK; /* POS=0, no stale START/STOP before a new transaction. */
    enum Fault fault = address(0xd0u);
    if(fault != OK) return fault;
    clear_address();
    fault = send(reg);
    if(fault != OK) return fault;
    if(count == 2u) I2C_CR1 |= POS; /* ACK stays set until the address phase. */
    fault = address(0xd1u); /* Pointer write retains BUSY: this is repeated START. */
    if(fault != OK) return fault;

    if(count == 1u)
    {
        /* RM0090 one-byte receive: ACK=0 BEFORE ADDR clear, STOP immediately
         * AFTER it. Keep the SR1/SR2/STOP sequence together across SysTick. */
        __asm volatile("cpsid i" ::: "memory");
        I2C_CR1 &= ~(ACK | POS);
        clear_address();
        I2C_CR1 |= STOP;
        __asm volatile("cpsie i" ::: "memory");
        fault = wait_flag(RXNE, TIMEOUT_RX, 0);
        if(fault != OK) return fault;
        i2c_probe.id = I2C_DR & 0xffu;
    }
    else if(count == 2u)
    {
        /* POS must precede ADDR clear. Both legal ACK orderings are kept
         * adjacent to SR1/SR2 so SysTick cannot split the critical sequence. */
        __asm volatile("cpsid i" ::: "memory");
#if I2C_PROBE_ACK_AFTER_ADDR
        clear_address();
        I2C_CR1 &= ~ACK;
#else
        I2C_CR1 &= ~ACK;
        clear_address();
#endif
        __asm volatile("cpsie i" ::: "memory");
        fault = wait_flag(BTF, TIMEOUT_RX, 0);
        if(fault != OK) return fault;
        __asm volatile("cpsid i" ::: "memory");
        I2C_CR1 |= STOP;
        i2c_probe.short_sample[0] = I2C_DR & 0xffu;
        i2c_probe.short_sample[1] = I2C_DR & 0xffu;
        I2C_CR1 &= ~POS;
        __asm volatile("cpsie i" ::: "memory");
        i2c_probe.short_sample_count = 2u;
    }
    else
    {
        clear_address();
        uint32_t index = 0u;
        while(count - index > 3u)
        {
            fault = wait_flag(RXNE, TIMEOUT_RX, 0);
            if(fault != OK) return fault;
            i2c_probe.sample[index++] = I2C_DR & 0xffu;
            i2c_probe.sample_count = index;
        }
        /* F4 last-three-byte sequence: BTF, ACK=0, read N-2; BTF, STOP,
         * read N-1 and N. This exercises both finite receive registers. */
        fault = wait_flag(BTF, TIMEOUT_RX, 0);
        if(fault != OK) return fault;
        __asm volatile("cpsid i" ::: "memory");
        I2C_CR1 &= ~ACK;
        i2c_probe.sample[index++] = I2C_DR & 0xffu;
        __asm volatile("cpsie i" ::: "memory");
        fault = wait_flag(BTF, TIMEOUT_RX, 0);
        if(fault != OK) return fault;
        __asm volatile("cpsid i" ::: "memory");
        I2C_CR1 |= STOP;
        i2c_probe.sample[index++] = I2C_DR & 0xffu;
        i2c_probe.sample[index++] = I2C_DR & 0xffu;
        __asm volatile("cpsie i" ::: "memory");
        i2c_probe.sample_count = index;
    }
    return wait_stop();
}

static enum Fault probe(void)
{
    enum Fault fault = read_registers(0x75u, 1u);
    if(fault != OK) return fault;
    if(i2c_probe.id != 0x68u) return BAD_ID;

    I2C_CR1 = PE | ACK;
    fault = address(0xd0u);
    if(fault != OK) return fault;
    clear_address();
    fault = send(0x6bu);
    if(fault != OK) return fault;
    fault = send(0u); /* PWR_MGMT_1: clear SLEEP, select internal oscillator. */
    if(fault != OK) return fault;
    I2C_CR1 |= STOP;
    fault = wait_stop();
    if(fault != OK) return fault;
    uint32_t wake = i2c_probe.ticks + 10u;
    while(i2c_probe.ticks < wake && i2c_probe.ticks < END_TICKS) {}
    i2c_probe.sample_count = 0u;
    fault = read_registers(0x3bu, 14u);
    if(fault != OK) return fault;
    for(uint32_t i = 0u; i < 14u; ++i)
        if(i2c_probe.sample[i] != expected_sample[i]) return BAD_SAMPLE;
    i2c_probe.short_sample_count = 0u;
    fault = read_registers(0x3bu, 2u);
    if(fault != OK) return fault;
    for(uint32_t i = 0u; i < 2u; ++i)
        if(i2c_probe.short_sample[i] != expected_sample[i]) return BAD_SAMPLE;
    return OK;
}

static void configure_i2c(uint32_t ccr)
{
    I2C_CR1 = 0u; /* Timing registers must be programmed with PE=0. */
    I2C_CR2 = 16u;
    I2C_OAR1 = 1u << 14;
    I2C_CCR = ccr;
    I2C_TRISE = 17u;
    I2C_CR1 = PE | ACK;
    i2c_probe.ccr = ccr;
}

#if I2C_PROBE_STOP_ONLY_RECOVERY
static void stop_only_retry(void)
{
    uint32_t status = I2C_SR1;
    I2C_CR1 = (I2C_CR1 & ~(START | ACK | POS)) | STOP | PE;
    I2C_SR1 = status & ~ERRORS;
    if(wait_stop() != OK)
    {
        i2c_probe.stop_timeout = 1u;
        report(TIMEOUT_STOP);
        return; /* Physically stuck bus: ordinary reset fallback remains below. */
    }
    i2c_probe.stop_only_completed = 1u;
    /* Two 100us ticks guarantee >=100us even when starting just before a tick.
     * Expired 30us responses must not reassert SDA after completed STOP.
     * Do not disable PE, assert SWRST, or write timing registers here. */
    uint32_t settle = i2c_probe.ticks + 2u;
    while(i2c_probe.ticks < settle && i2c_probe.ticks < END_TICKS) {}
    enum Fault fault = read_registers(0x75u, 1u);
    if(fault == OK && i2c_probe.id != 0x68u) fault = BAD_ID;
    i2c_probe.stop_only_fault = fault;
    if(fault == NACK_ADDRESS) console("I2C_STOP_ONLY_NACK\r\n");
    else if(fault == OK) console("I2C_STOP_ONLY_ID_GOOD\r\n");
    else report(fault);
}
#endif

static void recover(void)
{
    i2c_probe.fault_sr1 = I2C_SR1;
    i2c_probe.fault_sr2 = I2C_SR2;
    /* Request real STOP before clearing AF/BERR/ARLO (write-zero-to-clear).
     * A physically stuck clock cannot complete STOP: wait is bounded and the
     * subsequent PE/SWRST releases master state rather than claiming success. */
    I2C_CR1 = (I2C_CR1 & ~(START | ACK | POS)) | STOP | PE;
    I2C_SR1 = i2c_probe.fault_sr1 & ~ERRORS;
    i2c_probe.sr1_after_clear = I2C_SR1;
    if(wait_stop() != OK)
    {
        i2c_probe.stop_timeout = 1u;
        report(TIMEOUT_STOP);
    }
    I2C_CR1 = 0u;
    I2C_CR1 = SWRST;
    I2C_CR1 = 0u;
    i2c_probe.sr1_after_reset = I2C_SR1;
    i2c_probe.sr2_after_reset = I2C_SR2;
    configure_i2c(800u);
}

extern uint32_t __bss_start, __bss_end;
extern uint32_t __data_start, __data_end, __data_load;
static void reset_handler(void)
{
    for(uint32_t *p = &__bss_start; p < &__bss_end; ++p) *p = 0u;
    uint32_t *source = &__data_load;
    for(uint32_t *p = &__data_start; p < &__data_end; ++p) *p = *source++;
    SCB_VTOR = 0x08000000u;
    SYST_RVR = 1599u;
    SYST_CVR = 0u;
    SYST_CSR = 7u; /* 16MHz processor clock, interrupt, enable. */
    __asm volatile("cpsie i" ::: "memory");

    RCC_AHB1ENR |= 3u;
    RCC_APB1ENR |= 1u << 21;
    RCC_APB2ENR |= 1u << 4;
    (void)RCC_APB2ENR;
    MODER(GPIOA) = (MODER(GPIOA) & ~(3u << 18)) | (2u << 18);
    AFRH(GPIOA) = (AFRH(GPIOA) & ~(15u << 4)) | (7u << 4);
    MODER(GPIOB) = (MODER(GPIOB) & ~(15u << 12)) | (10u << 12);
    OTYPER(GPIOB) |= (1u << 6) | (1u << 7);
    OSPEEDR(GPIOB) |= 15u << 12;
    PUPDR(GPIOB) &= ~(15u << 12);
    AFRL(GPIOB) = (AFRL(GPIOB) & ~(255u << 24)) | (0x44u << 24);
    UART_BRR = 0x8bu;
    UART_CR1 = (1u << 13) | (1u << 3);
    RCC_APB1RSTR |= 1u << 21;
    RCC_APB1RSTR &= ~(1u << 21);
    configure_i2c(80u);
    i2c_probe.magic = 0x49324334u;
    i2c_probe.attempt = 1u;
    enum Fault fault = probe();
    i2c_probe.first_fault = fault;
    if(fault == OK)
    {
        i2c_probe.passed = 1u;
        console("I2C_GOOD\r\n");
    }
    else
    {
        report(fault);
#if I2C_PROBE_STOP_ONLY_RECOVERY
        stop_only_retry();
#endif
        recover();
        i2c_probe.attempt = 2u;
        fault = probe();
        i2c_probe.retry_fault = fault;
        if(fault == OK)
        {
            i2c_probe.passed = 2u;
            console("I2C_RECOVERED\r\n");
        }
        else
        {
            report(fault);
            I2C_CR1 = (I2C_CR1 & ~(START | ACK | POS)) | STOP | PE;
            I2C_SR1 = I2C_SR1 & ~ERRORS;
            (void)wait_stop();
            I2C_CR1 = 0u;
        }
    }
    console("I2C_DONE\r\n");
    while(i2c_probe.ticks < END_TICKS) __asm volatile("wfi");
    i2c_probe.completed = 1u;
    SYST_CSR = 0u;
    idle();
}

__attribute__((section(".vectors"), used))
const uintptr_t vectors[16] =
{
    [0] = 0x20020000u,
    [1] = (uintptr_t)reset_handler,
    [2] = (uintptr_t)idle,
    [3] = (uintptr_t)idle,
    [4] = (uintptr_t)idle,
    [5] = (uintptr_t)idle,
    [6] = (uintptr_t)idle,
    [15] = (uintptr_t)systick_handler
};
