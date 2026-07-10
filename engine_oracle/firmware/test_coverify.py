"""Firmware ↔ board co-verification tests (T-FIN6).

Seeded-violation fixture boards (strap conflict, direction clash, missing decouple, unknown
pin, input-only drive) + a clean demo board that must pass. Dynamic path is exercised with a
canned ``aicad.firmware.trace/1`` artifact (the T-FIN1 shape) — no emulator required.

Run:  pytest engine_oracle/firmware/test_coverify.py  (or as a module via main()).
"""

from __future__ import annotations

import json

from engine.ecad import schema
from engine.firmware import coverify, mcu_pinouts, pinmap
from engine.firmware import mcp_tools as fw_mcp


# ── fixtures ────────────────────────────────────────────────────────────────────


def _pad(num: str, net: str) -> dict:
    return {"id": f"p{num}", "number": num, "pos": [0.0, float(num or 0)], "angle": 0.0,
            "shape": "rect", "size": [0.5, 0.5], "type": "smd",
            "layers": ["F.Cu"], "drill": None, "net": net, "pin": num}


def _fp(fid: str, ref: str, value: str, fpid: str, pad_nets: list[str], **extra) -> dict:
    d = {"id": fid, "fpid": fpid, "ref": ref, "value": value, "pos": [10.0, 10.0],
         "angle": 0.0, "side": "top", "attr": "smd", "fields": {},
         "pads": [_pad(str(i + 1), n) for i, n in enumerate(pad_nets)],
         "graphics": [], "model_3d": None}
    d.update(extra)
    return d


def _board(nets: list[str], footprints: list[dict], name: str = "t_fin6") -> dict:
    b = schema.new_board(name)  # already carries GND
    for n in nets:
        if n != "GND":
            b["nets"].append({"name": n, "class": "default"})
    b["footprints"] = footprints
    return b


def _demo_board(*, with_cap: bool = True, extra_nets: list[str] | None = None,
                mcu_nets: list[str] | None = None, mcu_extra: dict | None = None) -> dict:
    """Clean STM32 demo board: LED on PA5, VDD decoupled, NRST/BOOT0 on their circuits."""
    mcu_nets = mcu_nets or ["PA5", "VDD", "GND", "NRST", "BOOT0"]
    nets = sorted(set(mcu_nets) | set(extra_nets or []))
    fps = [_fp("fp1", "U1", "STM32F407", "Package_QFP:LQFP-64", mcu_nets,
               **(mcu_extra or {})),
           _fp("fp2", "D1", "LED", "LED_SMD:LED_0603", ["PA5", "GND"]),
           _fp("fp3", "SW1", "RESET", "Button_SMD:SW", ["NRST", "GND"])]
    if with_cap:
        fps.append(_fp("fp4", "C1", "100n", "Capacitor_SMD:C_0402", ["VDD", "GND"]))
    return _board(nets, fps)


_BLINK_SRC = {
    "src/main.c": (
        "#include \"pinmap.h\"\n"
        "void main(void) {\n"
        "  gpio_set_dir(PIN_PA5, GPIO_OUT);\n"
        "  while (1) { HAL_GPIO_TogglePin(GPIOA, PIN_PA5); }\n"
        "}\n"),
}


def _trace(*pins: str) -> dict:
    """Canned aicad.firmware.trace/1 artifact with one toggle per pin."""
    events = []
    for i, p in enumerate(pins):
        events.append({"type": "gpio", "seq": i, "t_ms": float(i), "pin": p, "level": 1})
    return {"schema": "aicad.firmware.trace/1", "source": "renode", "generated_at": 0.0,
            "events": events, "gpio_final": {p: 1 for p in pins}}


def _rules(findings) -> set[str]:
    return {f["rule"] for f in findings}


# schematic with MCU pin + external push-pull driver both on net PA0
def _clash_schematic() -> dict:
    sch = schema.new_schematic("t_fin6_sch")
    sch["lib_cache"] = {
        "MCU:STM32F407": {
            "libid": "MCU:STM32F407", "name": "STM32F407", "description": "MCU",
            "reference_prefix": "U", "value": "STM32F407", "power": False,
            "pins": [{"number": "1", "name": "PA0", "pos": [0.0, 0.0], "angle": 0.0,
                      "length": 0.0, "type": "bidirectional", "unit": 1}],
        },
        "Sensor:OUT": {
            "libid": "Sensor:OUT", "name": "SENS", "description": "Sensor w/ push-pull out",
            "reference_prefix": "S", "value": "SENS", "power": False,
            "pins": [{"number": "1", "name": "OUT", "pos": [0.0, 0.0], "angle": 0.0,
                      "length": 0.0, "type": "output", "unit": 1}],
        },
    }
    sch["symbols"] = [
        {"id": "s1", "libid": "MCU:STM32F407", "sheet": "sh1", "ref": "U1",
         "value": "STM32F407", "footprint": "FP", "pos": [0.0, 0.0], "angle": 0.0,
         "mirror": "none", "unit": 1, "fields": {}, "in_bom": True, "on_board": True,
         "dnp": False},
        {"id": "s2", "libid": "Sensor:OUT", "sheet": "sh1", "ref": "S1",
         "value": "SENS", "footprint": "FP", "pos": [10.0, 0.0], "angle": 0.0,
         "mirror": "none", "unit": 1, "fields": {}, "in_bom": True, "on_board": True,
         "dnp": False},
    ]
    sch["wires"] = [{"id": "w1", "sheet": "sh1", "pts": [[0.0, 0.0], [10.0, 0.0]],
                     "bus": False}]
    sch["labels"] = [{"id": "l1", "sheet": "sh1", "pos": [5.0, 0.0], "text": "PA0",
                      "scope": "global"}]
    return sch


# ── source scan ─────────────────────────────────────────────────────────────────


def test_scan_firmware_pins():
    pins = coverify.scan_firmware_pins({
        "main.c": ("HAL_GPIO_WritePin(GPIOA, PIN_LED, 1);\n"
                   "int v = HAL_GPIO_ReadPin(GPIOB, PIN_SENSE);\n"
                   "pinMode(PIN_EN, OUTPUT);\n"
                   "x = PIN_MISC;\n"),
    })
    assert pins["LED"]["writes"] == 1 and pins["LED"]["reads"] == 0
    assert pins["SENSE"]["reads"] == 1 and pins["SENSE"]["writes"] == 0
    assert pins["EN"]["writes"] == 1          # OUTPUT pinMode counts as write intent
    assert pins["MISC"]["refs"] == 1 and pins["MISC"]["writes"] == 0
    print("  test_scan_firmware_pins OK")


# ── pin model enrichment ────────────────────────────────────────────────────────


def test_pin_model_flags_and_header_parity():
    board = _demo_board()
    model = pinmap.pin_model(board, mcu_family="cortex-m4")
    by_tok = {r["token"]: r for r in model}
    assert by_tok["BOOT0"]["strap"] and by_tok["NRST"]["strap"]
    assert not by_tok["PA5"]["strap"] and not by_tok["PA5"]["input_only"]
    assert by_tok["PA5"]["positional"] and by_tok["PA5"]["pin_id"] == 5
    assert by_tok["PA5"]["direction"] is None and by_tok["PA5"]["mux"] is None  # honest unknown
    # parity: every model token appears in the generated header and vice versa
    header = pinmap.generate_pinmap(board, mcu_family="cortex-m4")
    header_toks = {ln.split()[1][len("PIN_"):] for ln in header.splitlines()
                   if ln.startswith("#define PIN_")}
    assert header_toks == set(by_tok), (header_toks, set(by_tok))
    # ESP32 input-only
    assert mcu_pinouts.is_input_only_pin("GPIO34", "xtensa-lx6")
    assert not mcu_pinouts.is_input_only_pin("GPIO4", "xtensa-lx6")
    print("  test_pin_model_flags_and_header_parity OK")


# ── clean demo board ────────────────────────────────────────────────────────────


def test_clean_board_passes():
    board = _demo_board()
    fw_pins = coverify.scan_firmware_pins(_BLINK_SRC)
    findings = coverify.verify_board(board, fw_pins, sch=None,
                                     trace=_trace("PA5"))
    assert findings == [], findings
    print("  test_clean_board_passes OK")


def test_violation_shape_is_erc_compatible():
    board = _demo_board(with_cap=False)
    findings = coverify.verify_board(board, coverify.scan_firmware_pins(_BLINK_SRC))
    assert findings, "seeded board must produce findings"
    for f in findings:
        assert set(f) == {"rule", "severity", "net", "pins", "xy", "message", "fix"}
        assert f["severity"] in ("error", "warning")
        assert isinstance(f["pins"], list) and f["fix"]
    # purity + determinism
    snap = json.dumps(board, sort_keys=True)
    again = coverify.verify_board(board, coverify.scan_firmware_pins(_BLINK_SRC))
    assert json.dumps(board, sort_keys=True) == snap
    assert again == findings
    print("  test_violation_shape_is_erc_compatible OK")


# ── seeded static violations ────────────────────────────────────────────────────


def test_unknown_and_off_mcu_pins():
    board = _demo_board(extra_nets=["MISO"])   # MISO exists but not on an MCU pad
    src = {"main.c": "gpio_write(PIN_NOPE, 1); int v = gpio_read(PIN_MISO);\n"}
    findings = coverify.verify_board(board, coverify.scan_firmware_pins(src))
    assert "fw_pin_unknown_net" in _rules(findings)
    assert "fw_pin_not_on_mcu" in _rules(findings)
    unknown = [f for f in findings if f["rule"] == "fw_pin_unknown_net"][0]
    assert unknown["severity"] == "error" and "PIN_NOPE" in unknown["pins"]
    print("  test_unknown_and_off_mcu_pins OK")


def test_strap_conflict_board():
    board = _demo_board()
    src = {"main.c": "gpio_set_dir(PIN_BOOT0, GPIO_OUT); gpio_write(PIN_BOOT0, 1);\n"}
    findings = coverify.verify_board(board, coverify.scan_firmware_pins(src))
    strap = [f for f in findings if f["rule"] == "fw_strap_repurposed"]
    assert len(strap) == 1 and strap[0]["severity"] == "error"
    assert strap[0]["net"] == "BOOT0" and "boot" in strap[0]["message"].lower()
    print("  test_strap_conflict_board OK")


def test_direction_clash_board():
    board = _demo_board(mcu_nets=["PA0", "PA5", "VDD", "GND", "NRST", "BOOT0"])
    sch = _clash_schematic()
    src = {"main.c": "HAL_GPIO_WritePin(GPIOA, PIN_PA0, 1);\n"}
    findings = coverify.verify_board(board, coverify.scan_firmware_pins(src), sch=sch)
    clash = [f for f in findings if f["rule"] == "fw_direction_clash"]
    assert len(clash) == 1 and clash[0]["severity"] == "error"
    assert clash[0]["net"] == "PA0" and "s2.1" in clash[0]["pins"]
    # reading the same pin is fine — no clash
    src_rd = {"main.c": "int v = HAL_GPIO_ReadPin(GPIOA, PIN_PA0);\n"}
    ok = coverify.verify_board(board, coverify.scan_firmware_pins(src_rd), sch=sch)
    assert "fw_direction_clash" not in _rules(ok)
    # without a schematic the check degrades to skipped (honest, no guessing)
    no_sch = coverify.verify_board(board, coverify.scan_firmware_pins(src))
    assert "fw_direction_clash" not in _rules(no_sch)
    print("  test_direction_clash_board OK")


def test_missing_decouple_board():
    board = _demo_board(with_cap=False)
    findings = coverify.verify_board(board, coverify.scan_firmware_pins(_BLINK_SRC))
    dec = [f for f in findings if f["rule"] == "fw_missing_decouple"]
    assert len(dec) == 1 and dec[0]["net"] == "VDD" and dec[0]["severity"] == "warning"
    # T-FIN8 metadata rails: an eng-override rail (3V3) on the MCU with no cap is flagged too
    board2 = _demo_board(with_cap=True,
                         mcu_nets=["PA5", "VDD", "GND", "NRST", "BOOT0", "3V3"],
                         mcu_extra={"eng_overrides": {"supply": {"rails": [
                             {"name": "3V3", "voltage_v_nom": 3.3}]}}})
    findings2 = coverify.verify_board(board2, coverify.scan_firmware_pins(_BLINK_SRC))
    dec2 = [f for f in findings2 if f["rule"] == "fw_missing_decouple"]
    assert [d["net"] for d in dec2] == ["3V3"], dec2   # VDD is decoupled; 3V3 is not
    print("  test_missing_decouple_board OK")


def test_drive_input_only_esp32():
    nets = ["GPIO34", "GPIO4", "GND"]
    board = _board(nets, [_fp("fp1", "U1", "ESP32-WROOM", "RF_Module:ESP32", nets)])
    src = {"main.c": "gpio_write(PIN_GPIO34, 1); int v = gpio_read(PIN_GPIO4);\n"}
    findings = coverify.verify_board(board, coverify.scan_firmware_pins(src))
    ionly = [f for f in findings if f["rule"] == "fw_drive_input_only"]
    assert len(ionly) == 1 and ionly[0]["net"] == "GPIO34"
    print("  test_drive_input_only_esp32 OK")


# ── dynamic (trace) checks ──────────────────────────────────────────────────────


def test_trace_drive_conflict_and_unmapped():
    board = _demo_board(mcu_nets=["PA0", "PA5", "VDD", "GND", "NRST", "BOOT0"])
    sch = _clash_schematic()
    # observed: PA0 (externally driven), PA5 (fine), PB9 (no net on this board)
    findings = coverify.dynamic_checks(board, _trace("PA0", "PA5", "PB9"), sch=sch)
    rules = _rules(findings)
    assert rules == {"fw_trace_drive_conflict", "fw_trace_unmapped_pin"}, findings
    conf = [f for f in findings if f["rule"] == "fw_trace_drive_conflict"][0]
    assert conf["net"] == "PA0" and conf["severity"] == "error"
    unm = [f for f in findings if f["rule"] == "fw_trace_unmapped_pin"][0]
    assert unm["pins"] == ["PB9"] and unm["severity"] == "warning"
    # driving a strap pin observed live is a conflict even without a schematic
    strap = coverify.dynamic_checks(board, _trace("BOOT0"))
    assert _rules(strap) == {"fw_trace_drive_conflict"}
    print("  test_trace_drive_conflict_and_unmapped OK")


# ── fw_verify_board handler ─────────────────────────────────────────────────────


def test_fw_verify_board_handler(tmp_path):
    board = _demo_board(with_cap=False)
    board_path = tmp_path / "board.json"
    board_path.write_text(json.dumps(board), encoding="utf-8")
    trace_path = tmp_path / "fw.elf.trace.json"
    trace_path.write_text(json.dumps(_trace("BOOT0")), encoding="utf-8")

    res = fw_mcp.fw_verify_board({
        "board_doc_ref": str(board_path),
        "sources": dict(_BLINK_SRC),
        "trace_path": str(trace_path),
    })
    assert res.get("ok") is True, res
    assert res["mcu_family"] == "cortex-m4"
    assert res["dynamic"] == {"ran": True, "source": "trace"}
    rules = set(res["summary"]["rules"])
    assert "fw_missing_decouple" in rules and "fw_trace_drive_conflict" in rules
    assert res["summary"]["errors"] >= 1 and res["summary"]["warnings"] >= 1
    assert res["fw_pins"]["PA5"]["writes"] >= 1
    # pinmap.h defines are declarations, never uses
    res2 = fw_mcp.fw_verify_board({
        "board_doc_ref": str(board_path),
        "sources": {"include/pinmap.h": "#define PIN_BOOT0 8\n",
                    "src/main.c": "int x;\n"},
    })
    assert res2.get("ok") and res2["fw_pins"] == {}, res2
    # error envelopes
    assert fw_mcp.fw_verify_board({"board_doc_ref": "/nope"}).get("code") == "unknown_board"
    assert fw_mcp.fw_verify_board(
        {"board_doc_ref": str(board_path)}).get("code") == "invalid_request"
    print("  test_fw_verify_board_handler OK")


# ── module runner ───────────────────────────────────────────────────────────────


def main() -> None:
    import tempfile
    from pathlib import Path

    test_scan_firmware_pins()
    test_pin_model_flags_and_header_parity()
    test_clean_board_passes()
    test_violation_shape_is_erc_compatible()
    test_unknown_and_off_mcu_pins()
    test_strap_conflict_board()
    test_direction_clash_board()
    test_missing_decouple_board()
    test_drive_input_only_esp32()
    test_trace_drive_conflict_and_unmapped()
    with tempfile.TemporaryDirectory() as td:
        test_fw_verify_board_handler(Path(td))
    print("test_coverify: ALL OK")


if __name__ == "__main__":
    main()
