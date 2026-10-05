/* Same ELF for healthy, ACK-isolated, loaded/shorted and recovery runs.
 * Cortex-M4, HSI/APB1=16 MHz. Parent builds/runs; this fixture performs no host I/O.
 * Probe block starts at 0x20000000; all fields are uint32_t in declaration order.
 * Configure a 500 kbit/s peer, sample point .875, SJW=1. Inject IDs 0x111/0x222
 * to exercise both RX IRQs. Firmware queues ID 0x321 every 5 ms; an external
 * simultaneous ID 0x100 data frame wins arbitration without increasing TEC.
 * For physical confinement, declare a grounded low-impedance external 'fault'
 * endpoint, DriveExternal fault 0, then release (-1). ABOM observes 128x11
 * actual recessive bits. ACK isolation must plateau at TEC=128, not fake BOFF.
 * CAN_PROBE_LOOPBACK=1 sets BTR.LBKM (default 0 preserves external bus mode).
 * In that derivative, submit an external peer frame at 1 ms; SysTick issues
 * the first firmware mailbox at 2 ms, then at 5, 10, ... ms. Filter bank 2
 * accepts internal ID 0x321 into FIFO0; external queued frames must not block
 * later loopback mailboxes, nor appear as internal RX.
 * SRAM offsets from 0x20000000: command +0x40; tx_requests/first_tx_ms/last_tx_ms
 * +0x44/+0x48/+0x4c; tx_irq/last_tx_status/rx_count +0x50/+0x54/+0x58.
 * tx_status[32] at +0x5c captures raw TSR before W1C (mailbox0 RQCP=bit0,
 * TXOK=bit1, ALST=bit2, TERR=bit3; mailbox1/2 shifted by 8/16).
 * rx[32] at +0xdc has six uint32_t per entry: milliseconds, fifo, raw RIR,
 * raw RDTR (DLC in low nibble), low payload, high payload. Counts may exceed
 * array capacity; only the first 32 IRQ/status and RX records are retained.
 * Loopback TXOK plus RX ID 0x321/RDTR DLC=8/payload 44332211:88776655 on
 * successive requests proves continued operation; no console echo is used.
 */
#include <stdint.h>
#ifndef CAN_PROBE_LOOPBACK
#define CAN_PROBE_LOOPBACK 0
#endif
#define CAN_PROBE_RECORDS 32u
#define REG(a) (*(volatile uint32_t *)(a))
#define CAN(o) REG(0x40006400u + (o))
struct receive_record {
    uint32_t milliseconds, fifo, id, dtr, low, high;
};
struct probe_state {
    uint32_t ready, milliseconds, tx_ok, tx_error, arbitration_lost;
    uint32_t max_tec, max_rec, bus_off_seen, error_irq, rx0_irq, rx1_irq;
    uint32_t esr, tsr, received_id, received_low, received_high, command;
    uint32_t tx_requests, first_tx_ms, last_tx_ms, tx_irq, last_tx_status, rx_count;
    uint32_t tx_status[CAN_PROBE_RECORDS];
    struct receive_record rx[CAN_PROBE_RECORDS];
};
volatile struct probe_state probe __attribute__((section(".probe")));
static void snapshot(void)
{
    uint32_t esr = CAN(0x18), tsr = CAN(8);
    probe.esr = esr; probe.tsr = tsr;
    uint32_t tec = (esr >> 16) & 255, rec = esr >> 24;
    if(tec > probe.max_tec) probe.max_tec = tec;
    if(rec > probe.max_rec) probe.max_rec = rec;
    if(esr & 4) probe.bus_off_seen = 1;
    if(tsr & 0x00040404) probe.arbitration_lost = 1;
}
static void transmit(void)
{
    if(!(CAN(8) & (1u << 26))) return;
    CAN(0x184) = 8; CAN(0x188) = 0x44332211; CAN(0x18C) = 0x88776655;
    CAN(0x180) = (0x321u << 21) | 1;
    if(probe.tx_requests == 0) probe.first_tx_ms = probe.milliseconds;
    probe.last_tx_ms = probe.milliseconds;
    ++probe.tx_requests;
}
void SysTick_Handler(void)
{
    ++probe.milliseconds;
    snapshot();
    if(probe.command) {
        uint32_t command = probe.command; probe.command = 0;
        /* bit0 selects one-shot; bit1 disables automatic bus-off recovery. */
        CAN(0) = (command & 1 ? 0x10u : 0) | (command & 2 ? 0 : 0x40u);
    }
#if CAN_PROBE_LOOPBACK
    if(probe.milliseconds == 2 || probe.milliseconds % 5 == 0) transmit();
#else
    if(probe.milliseconds % 5 == 0) transmit();
#endif
}
void CAN_TX_Handler(void)
{
    snapshot();
    uint32_t tsr = CAN(8);
    uint32_t index = probe.tx_irq;
    probe.last_tx_status = tsr;
    if(index < CAN_PROBE_RECORDS) probe.tx_status[index] = tsr;
    probe.tx_irq = index + 1;
    for(uint32_t i = 0; i < 3; ++i) {
        uint32_t status = (tsr >> (8 * i)) & 15;
        if(status & 1) {
            if(status & 2) ++probe.tx_ok;
            if(status & 4) probe.arbitration_lost = 1;
            if(status & 8) ++probe.tx_error;
        }
    }
    CAN(8) = tsr & 0x00010101;
}
static void receive(uint32_t fifo)
{
    uint32_t status = fifo ? 0x10 : 0x0C, base = fifo ? 0x1C0 : 0x1B0;
    while(CAN(status) & 3) {
        uint32_t id = CAN(base), dtr = CAN(base + 4);
        uint32_t low = CAN(base + 8), high = CAN(base + 12);
        uint32_t index = probe.rx_count;
        probe.received_id = id; probe.received_low = low; probe.received_high = high;
        if(index < CAN_PROBE_RECORDS) {
            probe.rx[index].milliseconds = probe.milliseconds;
            probe.rx[index].fifo = fifo;
            probe.rx[index].id = id; probe.rx[index].dtr = dtr;
            probe.rx[index].low = low; probe.rx[index].high = high;
        }
        probe.rx_count = index + 1;
        CAN(status) = 0x20;
        if(fifo) ++probe.rx1_irq; else ++probe.rx0_irq;
    }
    CAN(status) = 0x18;
}
void CAN_RX0_Handler(void) { receive(0); }
void CAN_RX1_Handler(void) { receive(1); }
void CAN_SCE_Handler(void)
{
    ++probe.error_irq; snapshot(); CAN(4) = 0x1C; CAN(0x18) = 0x70;
}
void Default_Handler(void) { for(;;) {} }
void Reset_Handler(void)
{
    volatile uint32_t *words = (volatile uint32_t *)&probe;
    for(uint32_t i = 0; i < sizeof(probe) / sizeof(uint32_t); ++i) words[i] = 0;
    CAN(0) = 1; while(!(CAN(4) & 1)) {}
    CAN(0x1C) = 0x001C0001 | (CAN_PROBE_LOOPBACK ? (1u << 30) : 0u);
    /* BRP=2, TS1=13, TS2=2, SJW=1: 500 kbit/s; optional BTR.LBKM. */
    CAN(0x200) = 0xE01; CAN(0x20C) = 3; CAN(0x204) = 0;
    CAN(0x214) = 2; CAN(0x240) = 0x111u << 21; CAN(0x244) = 0xFFE00006;
    CAN(0x248) = 0x222u << 21; CAN(0x24C) = 0xFFE00006;
#if CAN_PROBE_LOOPBACK
    CAN(0x20C) = 7; CAN(0x250) = 0x321u << 21; CAN(0x254) = 0xFFE00006;
    CAN(0x21C) = 7;
#else
    CAN(0x21C) = 3;
#endif
    CAN(0x200) = 0xE00;
    CAN(0x14) = 0x8F7F; /* all TX/RX/error events, global ERRIE */
    REG(0xE000E100) = 0xFu << 19;
    CAN(0) = 0x40; while(CAN(4) & 1) {}
    REG(0xE000E014) = 15999; REG(0xE000E018) = 0; REG(0xE000E010) = 7;
    probe.ready = 0x43414E31;
#if !CAN_PROBE_LOOPBACK
    transmit();
#endif
    for(;;) __asm__ volatile("wfi");
}
__attribute__((section(".vectors"), used))
void (*const vectors[100])(void) = {
    [0] = (void (*)(void))0x20020000, [1] = Reset_Handler,
    [2] = Default_Handler, [3] = Default_Handler, [4] = Default_Handler,
    [5] = Default_Handler, [6] = Default_Handler, [15] = SysTick_Handler,
    [35] = CAN_TX_Handler, [36] = CAN_RX0_Handler,
    [37] = CAN_RX1_Handler, [38] = CAN_SCE_Handler
};
