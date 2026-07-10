---
id: T-FIN2
title: Emulation platform pack - UART+GPIO+ADC+I2C+SPI+timer peripherals across MCU families
workstream: FIN
owner: fable-fin
status: done
branch: task/T-FIN2-fw-peripheral-platforms
depends_on: [T-FIN1]
lane: ecad-firmware
kind: new
priority: 2
---

## Context

Only stm32f4_discovery.repl has a working UART; cortex_m_generic/esp32_stub are bare.
Build a curated Renode platform library so every supported family boots with usable
peripherals: STM32F4, STM32F1, nRF52, RP2040 (Renode ships upstream .repl bases for these —
survey and adapt rather than author from scratch, OSS-first). Each platform declares UART,
GPIO ports, ADC, I2C, SPI, timers where Renode models exist; document per-platform
capability honestly in platform_db. Decide + document the ESP32 story (Renode esp32 support
is weak; QEMU-xtensa or 'build+flash only, no emulation' are acceptable outcomes if stated).

## Files / areas touched

- engine_oracle/firmware/platforms/*.repl — new platform files
- engine_oracle/firmware/platform_db.py — capability matrix (uart/gpio/adc/i2c/spi/timers per family)
- engine_oracle/firmware/test_emulator.py — per-platform boot + hello-UART smoke (skips if renode absent)
- cad_guide('firmware') — supported-target table refresh

## Definition of done

- STM32F4/F1, nRF52, RP2040 platforms boot and print over UART in Renode
- platform_db exposes a queryable capability matrix; fw_sim_run errors honestly when an assertion needs a peripheral the platform lacks
- ESP32 path decided and documented (emulate via qemu-xtensa OR explicitly no-emulation)
- Per-platform smoke tests green locally with renode installed, skip cleanly without
