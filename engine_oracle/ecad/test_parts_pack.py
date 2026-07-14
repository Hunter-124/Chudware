"""Acceptance tests for the flight-computer + battery-charger ECAD parts pack.

Deterministic checks (no expected-time gating). Prints wall-times for each test.
"""

from __future__ import annotations

import sys
import time

from engine.ecad import library
from engine.ecad import netlist as netlist_mod
from engine.ecad import schema
from engine.ecad.library.starter import FOOTPRINTS, SYMBOLS


# New pack symbols and their default footprints (pins must be ⊆ pads).
NEW_PARTS: dict[str, str] = {
    "Sensor_Motion:MPU6050": "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm",
    "Sensor_Pressure:BMP280": "Package_LGA:LGA-8_2x2.5mm",
    "Sensor_Magnetic:LIS3MDL": "Package_LGA:LGA-12_2x2mm",
    "RF_GPS:NEO6M": "RF_Module:GPS_Castellated_1x6",
    "Regulator_Switching:Buck_3V3": "Package_TO_SOT_SMD:SOT-23-6",
    "Device:Crystal_32k768": "Crystal:Crystal_SMD_3215-2Pin",
    "Device:Crystal_HSE": "Crystal:Crystal_HC49-4H_Vertical",
    "Connector:SWD_2x5": "Connector_PinHeader_2.54mm:Header_2x05",
    "Battery_Management:MCP73831": "Package_TO_SOT_SMD:SOT-23-5",
    "Battery_Management:SwitchingCharger_SOP8": "Package_SO:SOIC-8",
    "Device:NMOS_Power": "Package_SO:SOIC-8",
    "Device:PMOS_Power": "Package_SO:SOIC-8",
    "Device:PMOS_Reverse": "Package_TO_SOT_SMD:SOT-23",
    "Device:R_Sense": "Resistor_SMD:R_2512",
    "Power_Management:Load_Switch": "Package_TO_SOT_SMD:SOT-23-5",
    "Power_Management:LVD_Comparator": "Package_TO_SOT_SMD:SOT-23-5",
    "Device:NTC": "Resistor_SMD:R_0603",
    "Device:L_Power": "Inductor_SMD:L_Power_7x7",
}

SEARCH_KEYWORDS = (
    "imu",
    "barometer",
    "magnetometer",
    "gps",
    "charger",
    "power_mosfet",
    "current_sense",
    "load_switch",
    "thermistor",
    "buck",
)

# Parts that consume/produce supply rails and must carry eng.supply.rails.
POWERED_NEW_PARTS = (
    "Sensor_Motion:MPU6050",
    "Sensor_Pressure:BMP280",
    "Sensor_Magnetic:LIS3MDL",
    "RF_GPS:NEO6M",
    "Regulator_Switching:Buck_3V3",
    "Battery_Management:MCP73831",
    "Battery_Management:SwitchingCharger_SOP8",
    "Power_Management:Load_Switch",
    "Power_Management:LVD_Comparator",
)


def _wall(label: str, t0: float) -> float:
    dt = time.perf_counter() - t0
    print(f"  WALL {label}: {dt:.4f}s")
    return dt


def test_search_keywords_return_hits() -> None:
    """(a) library.search / resolve path returns ≥1 pin-correct hit per keyword."""
    t0 = time.perf_counter()
    for q in SEARCH_KEYWORDS:
        hits = library.search(q)
        assert len(hits) >= 1, f"search({q!r}) returned 0 hits"
        # Prefer a symbol hit when present; otherwise any hit is enough for footprints.
        sym_hits = [h for h in hits if h.get("kind") == "symbol" and h.get("libid")]
        if sym_hits:
            libid = sym_hits[0]["libid"]
            sdef = library.resolve(libid)
            assert sdef is not None, f"resolve({libid!r}) after search({q!r}) is None"
            pins = sdef.get("pins") or []
            assert pins, f"{libid} has no pins (search {q!r})"
            # pin numbers must be unique non-empty strings
            nums = [str(p.get("number", "")) for p in pins]
            assert all(n for n in nums), f"{libid} has empty pin number"
        print(f"  ok  search({q!r}) → {len(hits)} hit(s)")
    _wall("test_search_keywords_return_hits", t0)


def test_new_symbol_pins_subset_of_footprint_pads() -> None:
    """(b) every NEW symbol's pin numbers ⊆ its footprint pad numbers."""
    t0 = time.perf_counter()
    for libid, fpid in NEW_PARTS.items():
        assert libid in SYMBOLS, f"missing symbol {libid}"
        assert fpid in FOOTPRINTS, f"missing footprint {fpid}"
        pin_nums = {str(p.get("number")) for p in SYMBOLS[libid].get("pins", [])}
        pad_nums = {str(p.get("number")) for p in FOOTPRINTS[fpid].get("pads", [])}
        missing = pin_nums - pad_nums
        assert not missing, (
            f"{libid} → {fpid}: pin numbers {sorted(missing)} not in pads {sorted(pad_nums)}"
        )
        # also resolve via public API
        assert library.resolve(libid) is not None
        assert library.resolve_footprint(fpid) is not None
    print(f"  ok  {len(NEW_PARTS)} new symbols: pins ⊆ pads")
    _wall("test_new_symbol_pins_subset_of_footprint_pads", t0)


def test_powered_parts_have_supply_eng() -> None:
    """(c) each powered new part carries eng supply-rail metadata."""
    t0 = time.perf_counter()
    for libid in POWERED_NEW_PARTS:
        sdef = SYMBOLS[libid]
        eng = sdef.get("eng") or {}
        supply = eng.get("supply") or {}
        rails = supply.get("rails") or []
        assert rails, f"{libid} missing eng.supply.rails: eng={eng!r}"
        for rail in rails:
            assert rail.get("name"), f"{libid} rail missing name"
            # at least one voltage figure present (honest nulls allowed for currents)
            assert any(
                rail.get(k) is not None
                for k in ("voltage_v_min", "voltage_v_nom", "voltage_v_max")
            ), f"{libid} rail {rail.get('name')!r} has no voltage figure"
        print(f"  ok  {libid} supply rails: {[r['name'] for r in rails]}")
    _wall("test_powered_parts_have_supply_eng", t0)


def _minimal_sch(name: str, components: list[dict], nets: dict[str, list[dict]]) -> dict:
    """Build a schematic with lib_cache + global labels realizing nets (no geometry needed)."""
    sch = schema.new_schematic(name)
    # place components on a grid; attach nets via global labels at pin world coords
    for i, c in enumerate(components):
        libid = c["libid"]
        sdef = library.resolve(libid)
        assert sdef is not None, f"unresolved libid {libid}"
        sch["lib_cache"][libid] = sdef
        x = (i % 8) * 25.4
        y = (i // 8) * 25.4
        sch["symbols"].append({
            "id": f"s{i+1}",
            "libid": libid,
            "sheet": "sh1",
            "ref": c["ref"],
            "value": c.get("value", sdef.get("value", "")),
            "footprint": c.get("footprint", ""),
            "pos": [x, y],
            "angle": 0.0,
            "mirror": "none",
            "unit": 1,
            "in_bom": True,
            "on_board": True,
            "dnp": False,
        })
    # Realize nets with global labels placed at each member pin's world endpoint.
    # schema.iter_placed_pins gives pin world positions after placement.
    pin_world: dict[tuple[str, str], list] = {}
    for sym, pin, world in schema.iter_placed_pins(sch):
        pin_world[(sym.get("ref", ""), str(pin.get("number", "")))] = list(world)

    lid = 0
    for net_name, members in nets.items():
        for m in members:
            key = (m["ref"], str(m["pin"]))
            pos = pin_world.get(key)
            assert pos is not None, f"pin not found for net {net_name}: {key}"
            lid += 1
            sch.setdefault("labels", []).append({
                "id": f"lbl{lid}",
                "sheet": "sh1",
                "pos": pos,
                "text": net_name,
                "shape": "global",
                "angle": 0.0,
            })
    return sch


def test_minimal_flight_computer_netlist_resolves() -> None:
    """(d) minimal flight-computer netlist from starter/pack parts resolves every part."""
    t0 = time.perf_counter()
    components = [
        {"ref": "U1", "libid": "MCU:Generic_QFP48", "value": "MCU"},
        {"ref": "U2", "libid": "Sensor_Motion:MPU6050", "value": "MPU6050"},
        {"ref": "U3", "libid": "Sensor_Pressure:BMP280", "value": "BMP280"},
        {"ref": "U4", "libid": "Sensor_Magnetic:LIS3MDL", "value": "LIS3MDL"},
        {"ref": "U5", "libid": "RF_GPS:NEO6M", "value": "NEO-6M"},
        {"ref": "U6", "libid": "Regulator_Switching:Buck_3V3", "value": "Buck_3V3"},
        {"ref": "U7", "libid": "Regulator_Linear:AMS1117-3.3", "value": "AMS1117-3.3"},
        {"ref": "Y1", "libid": "Device:Crystal_HSE", "value": "8MHz"},
        {"ref": "Y2", "libid": "Device:Crystal_32k768", "value": "32.768kHz"},
        {"ref": "J1", "libid": "Connector:SWD_2x5", "value": "SWD"},
        {"ref": "C1", "libid": "Device:C", "value": "100n"},
        {"ref": "C2", "libid": "Device:C", "value": "10u"},
        {"ref": "C3", "libid": "Device:C", "value": "100n"},
        {"ref": "L1", "libid": "Device:L_Power", "value": "4.7uH"},
        {"ref": "R1", "libid": "Device:R", "value": "10k"},
    ]
    # Resolve each component first (acceptance: every part resolves).
    for c in components:
        sdef = library.resolve(c["libid"])
        assert sdef is not None, f"flight-computer part unresolved: {c['libid']}"
        assert sdef.get("pins"), f"{c['libid']} has no pins"

    nets = {
        "+3V3": [
            {"ref": "U1", "pin": "13"},
            {"ref": "U2", "pin": "6"},
            {"ref": "U3", "pin": "8"},
            {"ref": "U4", "pin": "12"},
            {"ref": "U5", "pin": "1"},
            {"ref": "U6", "pin": "4"},  # FB net not 3V3; use VOUT-class via AMS1117
            {"ref": "U7", "pin": "2"},
            {"ref": "C1", "pin": "1"},
            {"ref": "C2", "pin": "1"},
            {"ref": "C3", "pin": "1"},
        ],
        "GND": [
            {"ref": "U1", "pin": "12"},
            {"ref": "U2", "pin": "7"},
            {"ref": "U3", "pin": "2"},
            {"ref": "U4", "pin": "6"},
            {"ref": "U5", "pin": "2"},
            {"ref": "U6", "pin": "3"},
            {"ref": "U7", "pin": "1"},
            {"ref": "C1", "pin": "2"},
            {"ref": "C2", "pin": "2"},
            {"ref": "C3", "pin": "2"},
            {"ref": "Y1", "pin": "2"},
            {"ref": "Y2", "pin": "2"},
        ],
        "I2C_SCL": [
            {"ref": "U2", "pin": "20"},
            {"ref": "U3", "pin": "4"},
            {"ref": "U4", "pin": "1"},
            {"ref": "R1", "pin": "1"},
        ],
        "I2C_SDA": [
            {"ref": "U2", "pin": "21"},
            {"ref": "U3", "pin": "3"},
            {"ref": "U4", "pin": "3"},
            {"ref": "R1", "pin": "2"},
        ],
        "UART_TX": [{"ref": "U5", "pin": "3"}],
        "UART_RX": [{"ref": "U5", "pin": "4"}],
        "SWDIO": [{"ref": "J1", "pin": "2"}],
        "SWCLK": [{"ref": "J1", "pin": "4"}],
        "HSE_1": [{"ref": "Y1", "pin": "1"}, {"ref": "U1", "pin": "10"}],
        "LSE_1": [{"ref": "Y2", "pin": "1"}],
        "VIN": [{"ref": "U6", "pin": "6"}, {"ref": "U7", "pin": "3"}],
        "SW_NODE": [{"ref": "U6", "pin": "2"}, {"ref": "L1", "pin": "1"}],
        "VOUT_BUCK": [{"ref": "L1", "pin": "2"}],
    }
    sch = _minimal_sch("flight_computer_min", components, nets)
    nl = netlist_mod.build_netlist(sch)
    refs = {c.get("ref") for c in nl.get("components", [])}
    for c in components:
        assert c["ref"] in refs, f"flight netlist missing component {c['ref']}"
        # each component's libid must still resolve
        assert library.resolve(c["libid"]) is not None
    # nets present
    for n in ("+3V3", "GND", "I2C_SCL", "I2C_SDA"):
        assert n in nl.get("nets", {}), f"flight netlist missing net {n}: {list(nl.get('nets', {}))}"
    print(f"  ok  flight-computer netlist: {len(components)} parts, "
          f"{len(nl.get('nets', {}))} nets")
    _wall("test_minimal_flight_computer_netlist_resolves", t0)


def test_minimal_charger_netlist_resolves() -> None:
    """(d) minimal battery-charger netlist from starter/pack parts resolves every part."""
    t0 = time.perf_counter()
    components = [
        {"ref": "U1", "libid": "Battery_Management:MCP73831", "value": "MCP73831"},
        {"ref": "U2", "libid": "Battery_Management:SwitchingCharger_SOP8", "value": "SW_CHG"},
        {"ref": "Q1", "libid": "Device:NMOS_Power", "value": "NMOS_PWR"},
        {"ref": "Q2", "libid": "Device:PMOS_Power", "value": "PMOS_PWR"},
        {"ref": "Q3", "libid": "Device:PMOS_Reverse", "value": "PMOS_REV"},
        {"ref": "RS1", "libid": "Device:R_Sense", "value": "0.05"},
        {"ref": "U3", "libid": "Power_Management:Load_Switch", "value": "LOAD_SW"},
        {"ref": "U4", "libid": "Power_Management:LVD_Comparator", "value": "LVD"},
        {"ref": "RT1", "libid": "Device:NTC", "value": "10k"},
        {"ref": "L1", "libid": "Device:L_Power", "value": "10uH"},
        {"ref": "C1", "libid": "Device:C", "value": "10u"},
        {"ref": "C2", "libid": "Device:C_pol", "value": "100u"},
        {"ref": "R1", "libid": "Device:R", "value": "2k"},
        {"ref": "D1", "libid": "Device:LED", "value": "LED"},
    ]
    for c in components:
        sdef = library.resolve(c["libid"])
        assert sdef is not None, f"charger part unresolved: {c['libid']}"
        assert sdef.get("pins"), f"{c['libid']} has no pins"

    nets = {
        "VIN": [
            {"ref": "U1", "pin": "4"},
            {"ref": "U2", "pin": "4"},
            {"ref": "Q3", "pin": "2"},
            {"ref": "C1", "pin": "1"},
            {"ref": "C2", "pin": "1"},
        ],
        "GND": [
            {"ref": "U1", "pin": "2"},
            {"ref": "U2", "pin": "3"},
            {"ref": "U3", "pin": "2"},
            {"ref": "U4", "pin": "2"},
            {"ref": "C1", "pin": "2"},
            {"ref": "C2", "pin": "2"},
            {"ref": "RT1", "pin": "2"},
            {"ref": "Q1", "pin": "1"},
        ],
        "VBAT": [
            {"ref": "U1", "pin": "3"},
            {"ref": "U2", "pin": "7"},
            {"ref": "RS1", "pin": "1"},
            {"ref": "U3", "pin": "1"},
        ],
        "VBAT_SENSE": [{"ref": "RS1", "pin": "2"}, {"ref": "Q2", "pin": "1"}],
        "SW": [{"ref": "U2", "pin": "5"}, {"ref": "L1", "pin": "1"}],
        "SW_OUT": [{"ref": "L1", "pin": "2"}],
        "STAT": [{"ref": "U1", "pin": "1"}, {"ref": "D1", "pin": "K"}, {"ref": "R1", "pin": "1"}],
        "STAT_PULL": [{"ref": "R1", "pin": "2"}, {"ref": "D1", "pin": "A"}],
        "PROG": [{"ref": "U1", "pin": "5"}],
        "TEMP": [{"ref": "U2", "pin": "1"}, {"ref": "RT1", "pin": "1"}],
        "LOAD_OUT": [{"ref": "U3", "pin": "5"}],
        "LVD_OUT": [{"ref": "U4", "pin": "1"}],
        "GATE_N": [{"ref": "Q1", "pin": "4"}],
        "GATE_P": [{"ref": "Q2", "pin": "4"}, {"ref": "Q3", "pin": "1"}],
        "REV_IN": [{"ref": "Q3", "pin": "3"}],
    }
    sch = _minimal_sch("battery_charger_min", components, nets)
    nl = netlist_mod.build_netlist(sch)
    refs = {c.get("ref") for c in nl.get("components", [])}
    for c in components:
        assert c["ref"] in refs, f"charger netlist missing component {c['ref']}"
        assert library.resolve(c["libid"]) is not None
    for n in ("VIN", "GND", "VBAT"):
        assert n in nl.get("nets", {}), f"charger netlist missing net {n}"
    print(f"  ok  charger netlist: {len(components)} parts, "
          f"{len(nl.get('nets', {}))} nets")
    _wall("test_minimal_charger_netlist_resolves", t0)


if __name__ == "__main__":
    # Allow running without pytest.
    tests = [
        test_search_keywords_return_hits,
        test_new_symbol_pins_subset_of_footprint_pads,
        test_powered_parts_have_supply_eng,
        test_minimal_flight_computer_netlist_resolves,
        test_minimal_charger_netlist_resolves,
    ]
    failed = 0
    t_all = time.perf_counter()
    for fn in tests:
        try:
            print(f"\n[{fn.__name__}]")
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL: {exc}", file=sys.stderr)
    print(f"\nTOTAL WALL: {time.perf_counter() - t_all:.4f}s  failed={failed}")
    sys.exit(1 if failed else 0)
