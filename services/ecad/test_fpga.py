"""Tests for the FPGA support pack: device model, bank/diff/clock/config
checks, and golden-file constraint exports (XDC/LPF/CSV).

Golden files live in ``golden/fpga/``. To regenerate after an intentional format
change: ``python -m services.ecad.test_fpga --regen`` (writes them from the fixtures).
"""

from __future__ import annotations

import os
import sys

from services.ecad import fpga

GOLDEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden", "fpga")


# ── fixtures ──────────────────────────────────────────────────────────────────────

def _nl(ref: str, libid: str, value: str, pins: dict, fields: dict | None = None) -> dict:
    """Minimal netlist dict shaped like netlist.build_netlist output."""
    nets: dict[str, list] = {}
    for i, (pin, net) in enumerate(pins.items()):
        nets.setdefault(net, []).append({"sym": "s1", "pin": pin, "ref": ref,
                                         "type": "bidirectional", "xy": [0, 0]})
        # every fixture net has a second member (a connector pin) so it counts as wired
        nets[net].append({"sym": "s9", "pin": str(i + 1), "ref": "J1",
                          "type": "passive", "xy": [10, 0]})
    return {
        "nets": dict(sorted(nets.items())),
        "by_pin": {f"s1.{p}": n for p, n in sorted(pins.items())},
        "components": [{"ref": ref, "libid": libid, "value": value, "footprint": "",
                        "sym": "s1", "pins": dict(pins), "fields": fields or {},
                        "dnp": False}],
        "warnings": [],
    }


def _ice40_good() -> dict:
    """A wired-correctly iCE40UP5K-SG48 demo board netlist (the golden-export fixture)."""
    return _nl("U1", "FPGA:iCE40UP5K-SG48", "iCE40UP5K-SG48", {
        # IO
        "35": "CLK12M",      # IOT_46B_G0 — clock-capable
        "46": "LED0",        # IOB_0A
        "2":  "UART_TX",     # IOB_6A
        "4":  "D_P",         # IOB_8A (pair IOB_8_9, p)
        "3":  "D_N",         # IOB_9B (pair IOB_8_9, n)
        # power / VCCIO
        "1":  "+3V3", "33": "+3V3", "22": "+3V3",
        "5":  "+1V2", "30": "+1V2",
        "49": "GND",
        # config
        "8":  "CRESET_B",
    })


def _xc7_demo() -> dict:
    """XC7A35T-CPG236 demo (Cmod A7-ish): clock on L17 (MRCC), LEDs, one diff pair."""
    return _nl("U2", "FPGA:XC7A35T-CPG236", "XC7A35T-CPG236", {
        "L17": "SYSCLK",     # IO_L12P_T1_MRCC_14
        "A17": "LED0",       # IO_L12N_T1_MRCC_16
        "C16": "LED1",       # IO_L13P_T2_MRCC_16
        "C17": "DATA_P",     # IO_L14P_T2_SRCC_16 (pair L14_16, p)
        "B17": "DATA_N",     # IO_L14N_T2_SRCC_16 (pair L14_16, n)
    })


# ── device registry ────────────────────────────────────────────────────────────────

def test_devices_load_and_schema():
    devs = fpga.load_devices()
    assert {"ice40up5k-sg48", "lfe5u-25f-cabga256", "xc7a35t-cpg236"} <= set(devs)
    for d in devs.values():
        assert d["schema"] == "aicad.fpga_device/1"
        assert d["constraint_dialect"] in ("xdc", "lpf")
        assert d["banks"] and d["pins"]
        for p in d["pins"].values():
            assert p["kind"] in ("io", "power", "config", "jtag", "special", "nc")
            if p["pair"]:
                assert p["pol"] in ("p", "n")


def test_ice40_pin_table_facts():
    d = fpga.get_device("iCE40UP5K-SG48")
    io = [p for p in d["pins"].values() if p["kind"] == "io"]
    assert len(io) == 36                      # icestorm pinloc-u4k-sg48 count
    clocks = sorted(k for k, p in d["pins"].items() if p["clock"])
    assert clocks == ["20", "35", "37", "44"]  # G3, G0, G1, G6
    # true pair example: IOB_8A (4) + IOB_9B (3)
    assert d["pins"]["4"]["pair"] == d["pins"]["3"]["pair"] is not None
    assert d["pins"]["4"]["pol"] == "p" and d["pins"]["3"]["pol"] == "n"


def test_device_resolution():
    assert fpga.get_device("ice40up5k-sg48")["device"] == "iCE40UP5K-SG48"
    assert fpga.get_device("XC7A35T")["device"] == "XC7A35T-CPG236"  # prefix, unambiguous
    assert fpga.get_device("nonsense") is None
    nl = _ice40_good()
    assert fpga.device_for_component(nl["components"][0])["device"] == "iCE40UP5K-SG48"


def test_starter_symbol_and_footprint():
    from services.ecad.library import starter
    sym = starter.get_symbol("FPGA:iCE40UP5K-SG48")
    assert sym is not None
    assert len(sym["pins"]) == 49
    # symbol pin numbers exactly match the device model
    dev = fpga.get_device("iCE40UP5K-SG48")
    assert {p["number"] for p in sym["pins"]} == set(dev["pins"])
    # no two pins share a connection point (would silently short in the netlist union-find)
    pts = {(p["pos"][0], p["pos"][1]) for p in sym["pins"]}
    assert len(pts) == 49
    fp = starter.get_footprint("Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm")
    assert fp is not None and len(fp["pads"]) == 49
    assert fp["eng"]["package"]["l_mm"] == 7.0


# ── checks ──────────────────────────────────────────────────────────────────────────

def test_clean_board_passes():
    res = fpga.fpga_check(_ice40_good())
    assert res["ok"], res["violations"]
    assert res["devices"][0]["device"] == "iCE40UP5K-SG48"


def test_bank_vccio_conflict_flagged():
    nl = _ice40_good()
    # bank 2 VCCIO dropped to 1.8 V but LED0 (bank 2) declared LVCMOS33
    nl["components"][0]["pins"]["1"] = "+1V8"
    res = fpga.fpga_check(nl, io={"LED0": {"iostandard": "LVCMOS33"}})
    kinds = [v["check"] for v in res["violations"]]
    assert "bank_vccio" in kinds
    v = next(v for v in res["violations"] if v["check"] == "bank_vccio")
    assert v["need_v"] == 3.3 and v["vccio_v"] == 1.8


def test_bank_vccio_net_name_inference():
    nl = _ice40_good()
    nl["components"][0]["pins"]["1"] = "+1V8"        # bank 2 VCCIO = 1.8 V
    nl["components"][0]["pins"]["46"] = "SENSOR_3V3"  # net name implies 3.3 V IO
    res = fpga.fpga_check(nl)
    assert any(v["check"] == "bank_vccio" and v.get("pin") == "46"
               for v in res["violations"])


def test_illegal_vccio_voltage_flagged():
    nl = _ice40_good()
    nl["components"][0]["pins"]["1"] = "+5V0"   # 5.0 V is not a legal iCE40UP VCCIO
    res = fpga.fpga_check(nl)
    assert any(v["check"] == "bank_vccio" and v.get("volts") == [5.0]
               for v in res["violations"])


def test_clock_on_non_clock_pin_flagged():
    nl = _ice40_good()
    nl["components"][0]["pins"]["46"] = "CLK_EXT"   # IOB_0A is not clock-capable
    res = fpga.fpga_check(nl)
    assert any(v["check"] == "clock" and v["pin"] == "46" for v in res["violations"])


def test_diff_pair_on_wrong_pins_flagged():
    nl = _ice40_good()
    pins = nl["components"][0]["pins"]
    pins["4"] = "D_P"
    pins["6"] = "D_N"      # IOB_13B — not IOB_8A's mate
    del pins["3"]
    res = fpga.fpga_check(nl)
    assert any(v["check"] == "diff_pair" for v in res["violations"])


def test_diff_pair_swapped_polarity_flagged():
    nl = _ice40_good()
    pins = nl["components"][0]["pins"]
    pins["4"], pins["3"] = "D_N", "D_P"    # right pair, backwards
    res = fpga.fpga_check(nl)
    assert any(v["check"] == "diff_pair" and "polarity" in v["message"]
               for v in res["violations"])


def test_config_pin_unwired_flagged():
    nl = _ice40_good()
    del nl["components"][0]["pins"]["8"]   # CRESET_B floating
    res = fpga.fpga_check(nl)
    assert any(v["check"] == "config" and v["role"] == "CRESET_B"
               for v in res["violations"])


def test_partial_seed_skips_unknown_checks():
    # ECP5 seed has no clock data: a "CLK" net must NOT be false-flagged.
    nl = _nl("U3", "FPGA:LFE5U-25F-CABGA256", "LFE5U-25F-CABGA256",
             {"A10": "CLK_IN", "A11": "D_P", "A12": "D_N"})
    res = fpga.fpga_check(nl)
    assert not any(v["check"] == "clock" for v in res["violations"])
    assert any("clock check skipped" in w for w in res["warnings"])
    # but its diff-pair data IS populated: A11/A12 share R0C53 A/B -> a true pair
    assert res["ok"], res["violations"]


def test_ecp5_diff_pair_violation():
    nl = _nl("U3", "FPGA:LFE5U-25F-CABGA256", "LFE5U-25F-CABGA256",
             {"A10": "D_N", "A11": "D_P"})   # different sites — not a pair
    res = fpga.fpga_check(nl)
    assert any(v["check"] == "diff_pair" for v in res["violations"])


def test_xc7_clock_and_pair():
    res = fpga.fpga_check(_xc7_demo())
    assert res["ok"], res["violations"]
    nl = _xc7_demo()
    nl["components"][0]["pins"]["A17"] = "CLK_BAD"   # L12N MRCC — actually clock-capable
    assert fpga.fpga_check(nl)["ok"]                  # MRCC pins pass the clock check
    nl["components"][0]["pins"]["C17"] = "CLK2"       # SRCC — also clock-capable
    assert fpga.fpga_check(nl)["ok"]


def test_unknown_ref_errors():
    res = fpga.fpga_check(_ice40_good(), ref="U9")
    assert not res["ok"] and res["violations"][0]["check"] == "device"


# ── exports (golden snapshots) ─────────────────────────────────────────────────────

def _golden(name: str, text: str, regen: bool = False) -> None:
    path = os.path.join(GOLDEN_DIR, name)
    if regen or os.environ.get("CHUDWARE_REGEN_GOLDEN"):
        os.makedirs(GOLDEN_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return
    with open(path, encoding="utf-8") as fh:
        assert text == fh.read(), f"{name} drifted from golden — regen only if intentional"


def test_golden_ice40_lpf_csv():
    out = fpga.export_constraints(_ice40_good())["U1"]
    assert set(out) == {"lpf", "csv"}
    _golden("ice40up5k_demo.lpf", out["lpf"])
    _golden("ice40up5k_demo.csv", out["csv"])


def test_golden_xc7_xdc_csv():
    out = fpga.export_constraints(
        _xc7_demo(), io={"SYSCLK": {"iostandard": "LVCMOS33"}})["U2"]
    assert set(out) == {"xdc", "csv"}
    _golden("xc7a35t_demo.xdc", out["xdc"])
    _golden("xc7a35t_demo.csv", out["csv"])


def test_export_determinism():
    a = fpga.export_constraints(_ice40_good())["U1"]["lpf"]
    b = fpga.export_constraints(_ice40_good())["U1"]["lpf"]
    assert a == b


def test_export_skips_non_io_pins():
    csv_text = fpga.export_constraints(_ice40_good())["U1"]["csv"]
    assert "CRESET_B" not in csv_text and "GND" not in csv_text  # config/power excluded
    assert "CLK12M" in csv_text and "yes" in csv_text            # clock flag carried


# ── schematic integration: placed starter symbol -> netlist -> check ───────────────

def test_schematic_roundtrip():
    from services.ecad import library, schema
    # The netlist is compiled C++-side; the facade resolves it from the doc's
    # owning session, so mint one around the inline doc first.
    from services.mcp.ecad_common import ecad_session as esess, netlist_mod as netlist
    doc = schema.new_schematic("fpga_demo")
    libid = "FPGA:iCE40UP5K-SG48"
    sdef = library.resolve(libid)
    assert sdef is not None and len(sdef["pins"]) == 49
    doc.setdefault("lib_cache", {})[libid] = sdef
    sym = {"id": "s1", "libid": libid, "ref": "U1",
           "value": "iCE40UP5K-SG48", "pos": [100.0, 100.0], "angle": 0.0,
           "mirror": "none", "unit": 1, "fields": {}, "sheet": doc["sheets"][0]["id"]}
    doc.setdefault("symbols", []).append(sym)
    sess = esess.open_session(doc)
    try:
        nl = netlist.build_netlist(sess.doc)
    finally:
        esess.close_session(sess.doc_id)
    comp = nl["components"][0]
    assert fpga.device_for_component(comp)["device"] == "iCE40UP5K-SG48"
    # nothing wired: CRESET_B must be flagged, and no pins were silently shorted
    res = fpga.fpga_check(nl)
    assert any(v["check"] == "config" for v in res["violations"])


if __name__ == "__main__":  # pragma: no cover — golden regeneration helper
    if "--regen" in sys.argv:
        os.environ["CHUDWARE_REGEN_GOLDEN"] = "1"
        test_golden_ice40_lpf_csv()
        test_golden_xc7_xdc_csv()
        print(f"regenerated goldens in {GOLDEN_DIR}")
