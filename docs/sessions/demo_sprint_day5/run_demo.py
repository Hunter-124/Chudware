#!/usr/bin/env python3
"""T-C5 demo runner — soil-moisture sensor-read firmware, day-5 sprint demo.

Drives the exact MCP tool sequence services/agent/prompts/firmware.md instructs an agent to
follow: fw_new_project -> (pin-map from the linked ECAD board) -> write main.c -> fw_build ->
fw_sim_run -> iterate. There's no live LLM session behind this run (the driver plays the "agent"
role directly, calling the real tool-handler functions) — the point is to prove the pipeline
mechanism (pin-map generation, build, emulation, assertion) actually works end to end, which is
independent of whether an LLM or this script issues the calls.

Usage: AI_CAD_FIRMWARE_DIR=<scratch dir> python3 docs/sessions/demo_sprint_day5/run_demo.py
Prints a JSON summary and the full UART log; writes firmware_run.log next to this script.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from engine.firmware import mcp_tools  # noqa: E402
from engine.firmware import pinmap as fw_pinmap  # noqa: E402
from engine.firmware import project as fw_project  # noqa: E402

# Synthetic ECAD board doc for the soil-moisture sensor PCB (contracts/firmware_rpc.md §2's
# board_doc shape: a dict with a "nets" list). No committed board_doc_ref exists yet for this
# demo product (docs/SPRINT_5DAY_PLAN.md references it, but Day 2's ECAD board was never
# committed) — this is the minimal net list the day-5 demo narrative needs: an ADC input for the
# soil probe and a UART pair for telemetry, on an STM32F4-class MCU.
BOARD_DOC = {
    "nets": [
        {"name": "PA0"},   # ADC1_IN0 — soil moisture probe analog input
        {"name": "PA9"},   # USART1_TX
        {"name": "PA10"},  # USART1_RX
        {"name": "VCC"},   # power (skipped by generate_pinmap)
        {"name": "GND"},   # ground (skipped by generate_pinmap)
    ]
}

MAIN_C = r'''/* T-C5 demo firmware: soil-moisture sensor-read loop.
 * Behavior spec: "read soil moisture via ADC every 5s, transmit over UART at 115200".
 * Pin map: build/include/pinmap.h (generated from the board's ECAD nets by
 * engine.firmware.pinmap.generate_pinmap — PIN_PA0/PIN_PA9/PIN_PA10 below).
 *
 * Register-level (no CMSIS/HAL vendored in this minimal template — see firmware.md): direct
 * STM32F4 peripheral register access for RCC/GPIOA/USART1/ADC1.
 */
#include <stdint.h>
#include "pinmap.h"

#define RCC_BASE      0x40023800u
#define RCC_AHB1ENR   (*(volatile uint32_t *)(RCC_BASE + 0x30u))
#define RCC_APB2ENR   (*(volatile uint32_t *)(RCC_BASE + 0x44u))

#define GPIOA_BASE    0x40020000u
#define GPIOA_MODER   (*(volatile uint32_t *)(GPIOA_BASE + 0x00u))
#define GPIOA_AFRH    (*(volatile uint32_t *)(GPIOA_BASE + 0x24u))  /* AFR[1]: pins 8-15 */

#define USART1_BASE   0x40011000u
#define USART1_SR     (*(volatile uint32_t *)(USART1_BASE + 0x00u))
#define USART1_DR     (*(volatile uint32_t *)(USART1_BASE + 0x04u))
#define USART1_BRR    (*(volatile uint32_t *)(USART1_BASE + 0x08u))
#define USART1_CR1    (*(volatile uint32_t *)(USART1_BASE + 0x0Cu))
#define USART_SR_TXE    (1u << 7)
#define USART_CR1_UE    (1u << 13)
#define USART_CR1_TE    (1u << 3)

#define ADC1_BASE     0x40012000u
#define ADC1_SR       (*(volatile uint32_t *)(ADC1_BASE + 0x00u))
#define ADC1_CR2      (*(volatile uint32_t *)(ADC1_BASE + 0x08u))
#define ADC1_SQR3     (*(volatile uint32_t *)(ADC1_BASE + 0x34u))
#define ADC1_DR       (*(volatile uint32_t *)(ADC1_BASE + 0x4Cu))
#define ADC_SR_EOC      (1u << 1)
#define ADC_CR2_ADON    (1u << 0)
#define ADC_CR2_SWSTART (1u << 30)

static void delay(volatile uint32_t n)
{
    while (n--) {
        __asm__ volatile("nop");
    }
}

static void uart_init(void)
{
    RCC_AHB1ENR |= (1u << 0);   /* GPIOA clock */
    RCC_APB2ENR |= (1u << 4);   /* USART1 clock */

    /* PIN_PA9 (TX) / PIN_PA10 (RX) -> alternate-function mode, AF7 (USART1). */
    GPIOA_MODER &= ~((3u << (PIN_PA9 * 2)) | (3u << (PIN_PA10 * 2)));
    GPIOA_MODER |=  ((2u << (PIN_PA9 * 2)) | (2u << (PIN_PA10 * 2)));
    GPIOA_AFRH  &= ~((0xFu << ((PIN_PA9 - 8) * 4)) | (0xFu << ((PIN_PA10 - 8) * 4)));
    GPIOA_AFRH  |=  ((7u    << ((PIN_PA9 - 8) * 4)) | (7u    << ((PIN_PA10 - 8) * 4)));

    /* 115200 baud @ 16 MHz HSI (default core clock; no PLL configured for this demo). */
    USART1_BRR = 0x8AEu;
    USART1_CR1 = USART_CR1_UE | USART_CR1_TE;
}

static void uart_putc(char c)
{
    while (!(USART1_SR & USART_SR_TXE)) { }
    USART1_DR = (uint32_t)c;
}

static void uart_puts(const char *s)
{
    while (*s) {
        uart_putc(*s++);
    }
}

static void uart_put_uint(unsigned v)
{
    char buf[6];
    int i = 0;
    if (v == 0) {
        uart_putc('0');
        return;
    }
    while (v && i < (int)sizeof(buf)) {
        buf[i++] = (char)('0' + (v % 10));
        v /= 10;
    }
    while (i--) {
        uart_putc(buf[i]);
    }
}

static void adc_init(void)
{
    RCC_APB2ENR |= (1u << 8);  /* ADC1 clock */
    ADC1_SQR3 = 0;             /* channel 0 (PIN_PA0 / ADC1_IN0) first in the sequence */
    ADC1_CR2 = ADC_CR2_ADON;
}

static unsigned adc_read_soil(void)
{
    ADC1_CR2 |= ADC_CR2_SWSTART;
    /* Bounded poll for end-of-conversion. NOTE: Renode's minimal
     * templates/renode_platforms/stm32f4_discovery.repl models only cpu/usart1/flash/sram — no
     * ADC1 peripheral — so under emulation this reads back 0 from the unmapped bus and EOC never
     * sets; the bound avoids hanging the demo on that platform. On real STM32F4 silicon (or a
     * fuller Renode platform with an ADC1 model) this reads a live conversion in microseconds. */
    for (unsigned tries = 0; tries < 1000u; tries++) {
        if (ADC1_SR & ADC_SR_EOC) {
            break;
        }
    }
    return ADC1_DR & 0xFFFu;
}

int main(void)
{
    uart_init();
    adc_init();

    for (;;) {
        unsigned soil = adc_read_soil();
        uart_puts("SOIL: ");
        uart_put_uint(soil);
        uart_puts("\r\n");
        delay(2000000u);  /* demo-scaled interval (~5s target on real silicon; kept short here so
                              the sim's UART assertion is reachable within fw_sim_run's timeout) */
    }
    return 0;
}
'''


def main() -> int:
    result: dict = {}

    # 1. fw_new_project — explicit mcu_family (cortex-m4, matches STM32F4) since no live ECAD
    #    session/file exists yet for this demo product to auto-derive it from.
    # mcu_family="stm32f4" (not the generic "cortex-m4" mcu_pinouts.mpn_to_family() would produce
    # for an auto-detected STM32 BOM part) so platform_db.get_platform_file() routes to the real
    # stm32f4_discovery.repl (with a modeled USART1) instead of the generic Cortex-M platform —
    # see work/tasks/T-C5.md driver notes for the cross-task family-naming mismatch this surfaced.
    proj_result = mcp_tools.fw_new_project({
        "name": "soil_moisture_demo",
        "mcu_family": "stm32f4",
        "toolchain": "cmake",
    })
    if "project_id" not in proj_result:
        print(json.dumps({"step": "fw_new_project", "error": proj_result}, indent=2))
        return 1
    project_id = proj_result["project_id"]
    result["fw_new_project"] = proj_result
    project = fw_project.load(project_id)

    # 2. Pin-map header from the (synthetic) board doc — the real T-C2 code path.
    pinmap_path = fw_pinmap.write_pinmap(project, BOARD_DOC, mcu_family=project.mcu_family)
    result["pinmap_path"] = str(pinmap_path)
    result["pinmap_header"] = pinmap_path.read_text(encoding="utf-8")

    # 3. Write the sensor-read firmware (the "agent" step: rewrite main() using the pin map).
    main_c_path = project.root_dir / "src" / "main.c"
    main_c_path.write_text(MAIN_C, encoding="utf-8")

    # 4. fw_build
    build_result = mcp_tools.fw_build({"project_id": project_id})
    result["fw_build"] = build_result
    if not build_result.get("success"):
        print(json.dumps(result, indent=2))
        return 1

    # 5. fw_sim_run — UART assertion. The assertion mini-language (evaluate_assertions,
    #    engine/firmware/emulator.py) is substring-only ("uart:<text>" / plain text treated the
    #    same); it does not support the regex form named in the task file's DoD bullet
    #    ("SOIL: [0-9]+"). Using the substring-compatible equivalent instead of changing that
    #    shared, tested contract unilaterally — see work/tasks/T-C5.md driver notes.
    sim_result = mcp_tools.fw_sim_run({
        "project_id": project_id,
        "assertions": ["SOIL:"],
        "timeout_s": 15,
    })
    result["fw_sim_run"] = sim_result

    print(json.dumps(result, indent=2))
    return 0 if sim_result.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
