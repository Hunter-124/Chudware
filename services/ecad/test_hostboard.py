"""Host-board rule pack tests — seeded violation + clean fixture per rule family.

House style: ``from __future__ import annotations``, plain ``main()`` + asserts, run as a module.
Fixtures are self-contained boards built from :func:`schema.new_board` with a STUB library (the
starter library carries no MCU/regulator/crystal parts — identification must be metadata- and
convention-driven, which is exactly what these fixtures exercise). Coordinates are mm.

Run:  python -m services.ecad.test_hostboard

Covers, per rule family: a clean board that trips NOTHING, plus one seeded board per rule id:
  host_decoupling_missing / _far / _via, host_bulk_cap,
  host_sequencing / _unparsed / _unverifiable (consuming eng.supply.sequencing),
  host_xtal_loadcap / _loadcap_match / _loop / _keepout,
  host_reset_pull / host_reset_cap, host_bootstrap_float / host_bootstrap_shared.
Also: determinism (byte-stable output), value parsing, and the honesty rule
(sequencing None == unknown -> silence; [] == known-none -> silence).
"""

from __future__ import annotations

import copy
import json

from services.ecad import schema
from services.ecad.hostboard_rules import parse_cap_value, run_hostcheck


# ── stub library ─────────────────────────────────────────────────────────────────

class StubLib:
    """Minimal resolver: symbol defs with named/typed pins; no footprint defs needed."""

    def __init__(self, symbols: dict) -> None:
        self._symbols = symbols

    def resolve(self, libid: str):
        return self._symbols.get(libid)

    def resolve_footprint(self, fpid: str):
        return None


def _pin(number: str, name: str, ptype: str) -> dict:
    return {"number": number, "name": name, "pos": [0, 0], "angle": 0.0,
            "length": 2.54, "type": ptype, "shape": "line", "unit": 1}


SYMBOLS = {
    "MCU:GENERIC": {"pins": [
        _pin("1", "VDD", "power_in"),
        _pin("2", "VSS", "power_in"),
        _pin("3", "NRST", "input"),
        _pin("4", "BOOT0", "input"),
        _pin("5", "OSC_IN", "input"),
        _pin("6", "OSC_OUT", "output"),
        _pin("7", "PA0", "bidirectional"),
    ]},
    "Regulator:GENERIC": {"pins": [
        _pin("1", "VIN", "power_in"),
        _pin("2", "VOUT", "power_out"),
        _pin("3", "EN", "input"),
        _pin("4", "PGOOD", "output"),
        _pin("5", "GND", "power_in"),
    ]},
    "Device:C": {"pins": [_pin("1", "~", "passive"), _pin("2", "~", "passive")]},
    "Device:R": {"pins": [_pin("1", "~", "passive"), _pin("2", "~", "passive")]},
    "Device:Crystal": {"pins": [_pin("1", "~", "passive"), _pin("2", "~", "passive")]},
}

LIB = StubLib(SYMBOLS)


# ── fixture builders ─────────────────────────────────────────────────────────────

def _pad(pid: str, number: str, pos, net) -> dict:
    return {"id": pid, "number": number, "pos": list(pos), "angle": 0.0, "shape": "rect",
            "size": [0.5, 0.5], "type": "smd", "layers": ["F.Cu"], "drill": None, "net": net}


_FPN = [0]


def _fp(ref: str, libid: str, pos, pad_nets: list, value: str = "",
        side: str = "top", eng_overrides: dict | None = None) -> dict:
    """Footprint with pads numbered 1..N at the fp origin (pin positions don't matter
    for these topology rules; only the fp position feeds the distance heuristics)."""
    _FPN[0] += 1
    fid = f"fp{_FPN[0]}"
    pads = [_pad(f"{fid}_p{i + 1}", str(i + 1), [0.0, 0.0], net)
            for i, net in enumerate(pad_nets)]
    fp = {"id": fid, "fpid": "Test:FP", "libid": libid, "ref": ref, "value": value,
          "pos": list(pos), "angle": 0.0, "side": side, "pads": pads, "graphics": [],
          "model_3d": None, "attr": "smd", "linked_symbol": None}
    if eng_overrides is not None:
        fp["eng_overrides"] = eng_overrides
    return fp


def _add_nets(board: dict, *names: str) -> None:
    for name in names:
        if not any(n.get("name") == name for n in board["nets"]):
            board["nets"].append({"name": name, "class": "default"})


MCU_ENG = {"supply": {
    "rails": [{"name": "3V3", "voltage_v_min": 2.0, "voltage_v_nom": 3.3,
               "voltage_v_max": 3.6, "current_a_active": 0.05, "current_a_sleep": None}],
    "voltage_domains": ["3V3"],
    "sequencing": ["VCORE before VIO"],
}}


def clean_board() -> dict:
    """A host board that passes EVERY rule family."""
    b = schema.new_board("hosttest")
    _add_nets(b, "3V3", "NRST", "BOOT", "XIN", "XOUT", "VCORE", "VIO", "PG1", "SIG")
    fps = b["footprints"]
    # MCU with full engineering metadata (sequencing requirement VCORE before VIO)
    fps.append(_fp("U1", "MCU:GENERIC", [0.0, 0.0],
                   ["3V3", "GND", "NRST", "BOOT", "XIN", "XOUT", "SIG"],
                   value="MCU", eng_overrides=MCU_ENG))
    # decoupling + bulk on 3V3, right next to the MCU
    fps.append(_fp("C1", "Device:C", [1.0, 0.0], ["3V3", "GND"], value="100n"))
    fps.append(_fp("C2", "Device:C", [2.0, 0.0], ["3V3", "GND"], value="10u"))
    # reset conditioning
    fps.append(_fp("R1", "Device:R", [1.0, 2.0], ["NRST", "3V3"], value="10k"))
    fps.append(_fp("C3", "Device:C", [1.5, 2.0], ["NRST", "GND"], value="100n"))
    # boot strap to GND through a resistor
    fps.append(_fp("R2", "Device:R", [1.0, 3.0], ["BOOT", "GND"], value="10k"))
    # crystal + matched load caps, close in
    fps.append(_fp("Y1", "Device:Crystal", [0.0, 5.0], ["XIN", "XOUT"], value="8MHz"))
    fps.append(_fp("C4", "Device:C", [1.0, 5.0], ["XIN", "GND"], value="12p"))
    fps.append(_fp("C5", "Device:C", [-1.0, 5.0], ["XOUT", "GND"], value="12p"))
    # sequenced regulators: U2 makes VCORE (PGOOD on PG1), U3 makes VIO (EN from PG1)
    fps.append(_fp("U2", "Regulator:GENERIC", [1.5, -2.0],
                   ["3V3", "VCORE", "3V3", "PG1", "GND"], value="REG-A"))
    fps.append(_fp("U3", "Regulator:GENERIC", [2.0, -2.0],
                   ["3V3", "VIO", "PG1", None, "GND"], value="REG-B"))
    return b


def _rules(violations: list[dict]) -> list[str]:
    return sorted({v["rule"] for v in violations})


def _drop(board: dict, ref: str) -> dict:
    board["footprints"] = [f for f in board["footprints"] if f["ref"] != ref]
    return board


def _fp_by_ref(board: dict, ref: str) -> dict:
    return next(f for f in board["footprints"] if f["ref"] == ref)


# ── tests ─────────────────────────────────────────────────────────────────────────

def test_clean_board_passes() -> None:
    v = run_hostcheck(clean_board(), library=LIB)
    assert v == [], f"clean board must trip nothing, got: {_rules(v)}: " \
                    + json.dumps(v, indent=1)


def test_violation_shape_and_determinism() -> None:
    b = _drop(clean_board(), "C1")
    v1 = run_hostcheck(b, library=LIB)
    v2 = run_hostcheck(copy.deepcopy(b), library=LIB)
    assert json.dumps(v1, sort_keys=True) == json.dumps(v2, sort_keys=True)
    for v in v1:
        assert set(v) == {"rule", "severity", "locs", "items", "message", "hint"}
        assert v["severity"] in ("error", "warning")
        assert v["hint"], "every finding carries an actionable fix hint"
        assert v["items"] == sorted(v["items"])


def test_decoupling_missing() -> None:
    v = run_hostcheck(_drop(clean_board(), "C1"), library=LIB)
    assert "host_decoupling_missing" in _rules(v)
    miss = [x for x in v if x["rule"] == "host_decoupling_missing"]
    assert any("U1" in x["items"] for x in miss)
    assert all(x["severity"] == "error" for x in miss)


def test_decoupling_far() -> None:
    b = clean_board()
    _fp_by_ref(b, "C1")["pos"] = [30.0, 0.0]
    v = run_hostcheck(b, library=LIB)
    assert "host_decoupling_far" in _rules(v)
    assert "host_decoupling_missing" not in _rules(v)


def test_decoupling_via_quality() -> None:
    b = clean_board()
    _fp_by_ref(b, "C1")["side"] = "bottom"
    v = run_hostcheck(b, library=LIB)
    assert "host_decoupling_via" in _rules(v)


def test_bulk_cap() -> None:
    v = run_hostcheck(_drop(clean_board(), "C2"), library=LIB)
    assert "host_bulk_cap" in _rules(v)
    # bulk value bound: a 100n cap does NOT satisfy the bulk requirement (C1 remains)
    blk = [x for x in v if x["rule"] == "host_bulk_cap"]
    assert all(x["severity"] == "warning" for x in blk)


def test_sequencing_clean_chain_recognized() -> None:
    # the clean board's EN<-PG1<-PGOOD(U2 on VCORE) chain satisfies "VCORE before VIO"
    v = run_hostcheck(clean_board(), library=LIB)
    assert not any(x["rule"].startswith("host_sequencing") for x in v)


def test_sequencing_broken_chain() -> None:
    b = clean_board()
    # U3's EN pin straight to 3V3: VIO rises with the input rail, ignoring VCORE
    _fp_by_ref(b, "U3")["pads"][2]["net"] = "3V3"
    v = run_hostcheck(b, library=LIB)
    seq = [x for x in v if x["rule"] == "host_sequencing"]
    assert seq and all(x["severity"] == "error" for x in seq)
    assert any("VCORE" in x["message"] and "VIO" in x["message"] for x in seq)


def test_sequencing_en_direct_to_rail_a_is_ok() -> None:
    b = clean_board()
    # EN tied directly to the VCORE net is also a valid ordering mechanism
    _fp_by_ref(b, "U3")["pads"][2]["net"] = "VCORE"
    v = run_hostcheck(b, library=LIB)
    assert not any(x["rule"] == "host_sequencing" for x in v)


def test_sequencing_unparsed() -> None:
    b = clean_board()
    _fp_by_ref(b, "U1")["eng_overrides"] = {
        "supply": {"sequencing": ["power up carefully"]}}
    v = run_hostcheck(b, library=LIB)
    assert "host_sequencing_unparsed" in _rules(v)


def test_sequencing_unverifiable() -> None:
    b = _drop(clean_board(), "U3")  # nothing sources VIO any more
    v = run_hostcheck(b, library=LIB)
    assert "host_sequencing_unverifiable" in _rules(v)


def test_sequencing_honesty_none_and_empty_are_silent() -> None:
    for seq in (None, []):
        b = clean_board()
        _fp_by_ref(b, "U1")["eng_overrides"] = {"supply": {"sequencing": seq}}
        v = run_hostcheck(b, library=LIB)
        assert not any(x["rule"].startswith("host_sequencing") for x in v), seq


def test_xtal_loadcap_missing() -> None:
    v = run_hostcheck(_drop(clean_board(), "C4"), library=LIB)
    assert "host_xtal_loadcap" in _rules(v)


def test_xtal_loadcap_mismatch() -> None:
    b = clean_board()
    _fp_by_ref(b, "C5")["value"] = "22p"
    v = run_hostcheck(b, library=LIB)
    assert "host_xtal_loadcap_match" in _rules(v)


def test_xtal_loop_area() -> None:
    b = clean_board()
    _fp_by_ref(b, "C4")["pos"] = [15.0, 5.0]
    v = run_hostcheck(b, library=LIB)
    assert "host_xtal_loop" in _rules(v)


def test_xtal_keepout() -> None:
    b = clean_board()
    b["tracks"].append({"id": "t1", "net": "SIG", "layer": "F.Cu", "width": 0.25,
                        "kind": "segment", "p1": [-2.0, 5.2], "p2": [2.0, 5.2]})
    v = run_hostcheck(b, library=LIB)
    assert "host_xtal_keepout" in _rules(v)
    # a GND track that close is fine (return path, not an aggressor)
    b2 = clean_board()
    b2["tracks"].append({"id": "t1", "net": "GND", "layer": "F.Cu", "width": 0.25,
                         "kind": "segment", "p1": [-2.0, 5.2], "p2": [2.0, 5.2]})
    assert "host_xtal_keepout" not in _rules(run_hostcheck(b2, library=LIB))


def test_reset_pull_and_cap() -> None:
    v = run_hostcheck(_drop(clean_board(), "R1"), library=LIB)
    assert "host_reset_pull" in _rules(v)
    v = run_hostcheck(_drop(clean_board(), "C3"), library=LIB)
    assert "host_reset_cap" in _rules(v)


def test_bootstrap_float() -> None:
    v = run_hostcheck(_drop(clean_board(), "R2"), library=LIB)
    assert "host_bootstrap_float" in _rules(v)


def test_bootstrap_shared() -> None:
    b = clean_board()
    # another MCU's OSC_OUT (output pin, number 6) driving the BOOT net
    b["footprints"].append(_fp("U9", "MCU:GENERIC", [8.0, 8.0],
                               [None, None, None, None, None, "BOOT", None],
                               eng_overrides={"supply": {"sequencing": []}}))
    v = run_hostcheck(b, library=LIB)
    assert "host_bootstrap_shared" in _rules(v)


def test_non_board_and_empty_are_silent() -> None:
    assert run_hostcheck({}, library=LIB) == []
    assert run_hostcheck(schema.new_schematic(), library=LIB) == []
    assert run_hostcheck(schema.new_board(), library=LIB) == []


def test_parse_cap_value() -> None:
    def _close(a, b):
        return a is not None and abs(a - b) <= 1e-9 * abs(b)

    assert _close(parse_cap_value("100n"), 1e-7)
    assert _close(parse_cap_value("100nF"), 1e-7)
    assert _close(parse_cap_value("4.7u"), 4.7e-6)
    assert _close(parse_cap_value("12p"), 1.2e-11)
    assert _close(parse_cap_value("0.1uF"), 1e-7)
    assert _close(parse_cap_value("10k"), 1e4)  # generic SI (resistors reuse the parser)
    assert parse_cap_value("") is None
    assert parse_cap_value("MCU") is None
    assert parse_cap_value(None) is None


def test_threshold_overrides() -> None:
    b = clean_board()
    # tighten the decoupling budget below C1's 1.0 mm distance -> far warning appears
    v = run_hostcheck(b, library=LIB, opts={"decoupling_dist_mm": 0.5})
    assert "host_decoupling_far" in _rules(v)
    # per-board override path
    b["rules"]["hostcheck"] = {"decoupling_dist_mm": 0.5}
    assert "host_decoupling_far" in _rules(run_hostcheck(b, library=LIB))


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        _FPN[0] = 0
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} host-board rule tests passed")


if __name__ == "__main__":
    main()
