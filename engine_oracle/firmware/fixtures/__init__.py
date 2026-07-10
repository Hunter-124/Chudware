"""Sensor scenario fixture library — aicad.fwscenario/1 archetypes (T-FIN3).

Ready-made stimulus scenarios for common sensor archetypes, addressable from
``fw_sim_run(scenario="fixture:<name>")``. Each fixture is a plain
``aicad.fwscenario/1`` dict (see ``contracts/firmware_rpc.md`` §9 and
``engine.firmware.scenario``); :func:`get_fixture` returns a deep copy so callers can
tweak values (temperature, press times, waveform) without mutating the library.

Archetypes shipped (DoD: >= 4):
- ``i2c_temp_tmp103``   — I2C temperature sensor (upstream Renode Sensors.TMP103 @ 0x70)
- ``spi_imu_stub``      — SPI IMU connectivity stub (Mocks.DummySPISlave on spi1)
- ``quadrature_encoder``— 2-channel quadrature encoder on PA0/PA1 (24 steps CW, 5 ms/step)
- ``adc_thermistor``    — 10k NTC thermistor heat-up curve on the ADC stub (beta model)
- ``user_button``       — debounced user-button presses on PA0 (bonus archetype)
"""
from __future__ import annotations

import copy
import math

from engine.firmware.scenario import SCHEMA


def _thermistor_counts(t_start_c: float = 20.0, t_end_c: float = 60.0, n: int = 32,
                       beta: float = 3950.0, r0: float = 10_000.0, t0_c: float = 25.0,
                       r_fixed: float = 10_000.0) -> list[int]:
    """12-bit ADC counts for an NTC thermistor (beta model) in a divider with
    ``r_fixed`` to VCC, sensor to GND — a linear heat-up from t_start to t_end."""
    counts: list[int] = []
    for i in range(n):
        t_c = t_start_c + (t_end_c - t_start_c) * i / (n - 1)
        t_k, t0_k = t_c + 273.15, t0_c + 273.15
        r = r0 * math.exp(beta * (1.0 / t_k - 1.0 / t0_k))
        counts.append(max(0, min(4095, int(round(4095.0 * r / (r + r_fixed))))))
    return counts


_FIXTURES: dict[str, dict] = {
    "i2c_temp_tmp103": {
        "schema": SCHEMA,
        "name": "I2C temperature sensor (TMP103 @ 0x70, 25.5 °C)",
        "tracks": [
            {"kind": "i2c_sensor", "model": "tmp103", "bus": "i2c1",
             "address": 0x70, "temperature_c": 25.5},
        ],
    },
    "spi_imu_stub": {
        "schema": SCHEMA,
        "name": "SPI IMU connectivity stub on spi1",
        "tracks": [
            {"kind": "spi_stub", "bus": "spi1", "name": "aicadImu"},
        ],
    },
    "quadrature_encoder": {
        "schema": SCHEMA,
        "name": "Quadrature encoder PA0/PA1 — 24 steps CW @ 5 ms/step",
        "tracks": [
            {"kind": "quadrature", "pin_a": "PA0", "pin_b": "PA1",
             "start_ms": 100.0, "steps": 24, "step_ms": 5.0, "direction": "cw"},
        ],
    },
    "adc_thermistor": {
        "schema": SCHEMA,
        "name": "ADC thermistor heat-up 20→60 °C (10k NTC beta=3950, 10k divider)",
        "tracks": [
            {"kind": "adc_waveform", "channel": 0, "sample_ms": 10.0,
             "waveform": {"type": "samples", "values": _thermistor_counts()}},
        ],
    },
    "user_button": {
        "schema": SCHEMA,
        "name": "User button on PA0 — three 80 ms presses",
        "tracks": [
            {"kind": "button", "pin": "PA0", "presses": [
                {"t_ms": 200.0, "hold_ms": 80.0},
                {"t_ms": 600.0, "hold_ms": 80.0},
                {"t_ms": 1000.0, "hold_ms": 80.0},
            ]},
        ],
    },
}


def list_fixtures() -> list[str]:
    """Sorted fixture names."""
    return sorted(_FIXTURES)


def get_fixture(name: str) -> dict | None:
    """Deep copy of the named fixture scenario, or None when unknown."""
    fx = _FIXTURES.get(name)
    return copy.deepcopy(fx) if fx is not None else None
