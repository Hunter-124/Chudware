"""Tests for the ECAD-netlist → pin-map header + auto-MCU selection (T-C2)."""

from __future__ import annotations

import pytest

from engine.ecad import schema
from engine.firmware import mcu_pinouts, pinmap
from engine.firmware import project as fw_project


# --------------------------------------------------------------------------- fixtures


def _board_with_nets(*net_names: str, mcu_value: str = "STM32F103") -> dict:
    """Minimal board doc carrying the given nets plus one MCU footprint (mirrors
    engine/ecad/test_integration.py:_build_board)."""
    board = schema.new_board("t_c2")          # nets already contains {"name": "GND", ...}
    for nm in net_names:
        board["nets"].append({"name": nm, "class": "default"})
    board["footprints"].append({
        "id": "fp1", "fpid": "MCU:LQFP48", "ref": "U1", "value": mcu_value,
        "pos": [10.0, 10.0], "angle": 0.0, "side": "top", "fields": {},
        "pads": [
            {"id": "p1", "number": "1", "pos": [0.0, 1.0], "shape": "rect",
             "type": "smd", "layers": ["F.Cu"], "net": net_names[0] if net_names else "GND"},
        ],
    })
    return board


# --------------------------------------------------------------------------- generate_pinmap


def test_generate_pinmap_contains_expected_defines():
    """The task's headline assertion: nets PA5 + MISO produce their #defines."""
    board = _board_with_nets("PA5", "MISO")
    header = pinmap.generate_pinmap(board)
    assert "#define PIN_PA5" in header
    assert "#define PIN_MISO" in header


def test_generate_pinmap_is_a_valid_guarded_header():
    board = _board_with_nets("PA5", "MISO")
    header = pinmap.generate_pinmap(board)
    assert "#ifndef AICAD_PINMAP_H" in header
    assert "#define AICAD_PINMAP_H" in header
    assert header.rstrip().endswith("#endif /* AICAD_PINMAP_H */")


def test_power_and_ground_nets_are_skipped():
    board = _board_with_nets("PA5", "GND", "+3V3", "VCC", "5V")
    header = pinmap.generate_pinmap(board)
    assert "#define PIN_PA5" in header
    for rail in ("PIN_GND", "PIN__3V3", "PIN_VCC", "PIN_5V"):
        assert rail not in header, f"power rail {rail} must not appear in the pin-map"


def test_positional_net_decodes_to_stable_pin_id():
    """STM32 PA5 → port A(0)*16 + 5 = 5; a named net (MISO) gets a logical index >= base."""
    board = _board_with_nets("PA5", "MISO")
    header = pinmap.generate_pinmap(board)
    assert "#define PIN_PA5 5" in header
    # MISO has no positional encoding -> logical index >= LOGICAL_PIN_BASE
    miso_line = next(l for l in header.splitlines() if l.startswith("#define PIN_MISO"))
    miso_val = int(miso_line.split()[2])
    assert miso_val >= mcu_pinouts.LOGICAL_PIN_BASE


def test_net_name_is_sanitised_to_a_legal_macro():
    board = _board_with_nets("GPIO_LED", "SENSOR-DATA")
    header = pinmap.generate_pinmap(board)
    assert "#define PIN_GPIO_LED" in header
    assert "#define PIN_SENSOR_DATA" in header   # '-' -> '_'


def test_empty_board_produces_valid_header():
    board = schema.new_board("empty")  # only GND, which is skipped
    header = pinmap.generate_pinmap(board)
    assert "#ifndef AICAD_PINMAP_H" in header
    assert "no GPIO/signal nets" in header


# --------------------------------------------------------------------------- resolve_pin table


@pytest.mark.parametrize("name,expected", [
    ("PA5", 5),        # STM32 port A pin 5
    ("PA0", 0),
    ("PB12", 16 + 12),  # port B -> 16 + 12
    ("PC7", 32 + 7),
    ("GPIO4", 4),      # ESP32
    ("IO21", 21),
    ("D13", 13),       # Arduino digital
    ("MISO", None),    # named signal net, no encoding
    ("GPIO_LED", None),
    ("GND", None),
])
def test_resolve_pin(name, expected):
    assert mcu_pinouts.resolve_pin(name) == expected


# --------------------------------------------------------------------------- MCU family selection


@pytest.mark.parametrize("mpn,family", [
    ("STM32F103", "cortex-m4"),
    ("STM32F407VGTx", "cortex-m4"),
    ("MCU_ST_STM32F4:STM32F407VGTx", "cortex-m4"),
    ("ESP32-WROOM-32", "xtensa-lx6"),
    ("ATmega328P", "avr"),
    ("ATtiny85", "avr"),
    ("NE555", None),
    ("", None),
    (None, None),
])
def test_mpn_to_family(mpn, family):
    assert mcu_pinouts.mpn_to_family(mpn) == family


def test_select_mcu_family_from_board_bom():
    board = _board_with_nets("PA5", mcu_value="STM32F103")
    assert mcu_pinouts.select_mcu_family(board) == "cortex-m4"


def test_select_mcu_family_none_when_no_mcu():
    board = schema.new_board("no_mcu")
    board["footprints"].append({
        "id": "fp1", "fpid": "R:0805", "ref": "R1", "value": "10k",
        "pos": [0.0, 0.0], "angle": 0.0, "side": "top", "fields": {}, "pads": [],
    })
    assert mcu_pinouts.select_mcu_family(board) is None


def test_select_mcu_family_reads_custom_mpn_field():
    board = schema.new_board("mpn_field")
    board["footprints"].append({
        "id": "fp1", "fpid": "MCU:QFN", "ref": "U1", "value": "?",
        "pos": [0.0, 0.0], "angle": 0.0, "side": "top",
        "fields": {"MPN": "ESP32-S0WD"}, "pads": [],
    })
    assert mcu_pinouts.select_mcu_family(board) == "xtensa-lx6"


# --------------------------------------------------------------------------- write_pinmap


def test_write_pinmap_writes_to_build_include(tmp_path):
    board = _board_with_nets("PA5", "MISO")
    project = fw_project.new_project(None, "cortex-m4", root=tmp_path / "proj")
    out = pinmap.write_pinmap(project, board)
    assert out == project.build_path / "include" / "pinmap.h"
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    assert "#define PIN_PA5" in text and "#define PIN_MISO" in text


def test_schematic_nets_via_build_netlist_do_not_crash():
    """generate_pinmap tolerates a schematic doc (computed netlist), not just a board."""
    sch = schema.new_schematic("s")
    header = pinmap.generate_pinmap(sch)  # empty schematic -> no nets, but valid header
    assert "#ifndef AICAD_PINMAP_H" in header
