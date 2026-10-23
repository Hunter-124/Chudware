"""ngspice backend gates — plain main()+asserts (house style).

Contract under test:
  * backend='mna' (default) is untouched and identical to the pre-seam behaviour.
  * When ngspice is ABSENT: backend='ngspice' degrades HONESTLY to mna with a
    backend_note; nothing raises. (Forced via CHUDWARE_NGSPICE=<bogus> so this gate
    always runs.)
  * When ngspice is PRESENT: RC filter + resistor divider produce identical
    (within tolerance) results across backends for op/dc/tran/ac/tf, and a vendor
    .subckt op-amp attached to a component simulates through ngspice while auto
    routes to it. These live gates SKIP CLEANLY when ngspice is not installed.

Run:  python3 -m services.sim.test_ngspice_backend
"""
from __future__ import annotations

import math
import os
import tempfile
import time

# engine.sim.circuit.simulate -> C++ sim_spice RPC via the ecad_common
# facade; the ngspice subprocess backend (no C++ counterpart) lives in
# services/installer/ngspice_backend.py.
from services.mcp.ecad_common import _sim_run as simulate
from services.installer import ngspice_backend as ng


# --------------------------------------------------------------------------- helpers

def _pct_err(got: float, exp: float) -> float:
    if exp == 0:
        return abs(got)
    return abs(got - exp) / abs(exp) * 100.0


def _divider() -> dict:
    # 10V across R1=1k (top) / R2=2k (bottom): V(OUT)=6.667V, I(V1)=-3.333mA.
    return {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 10.0,
             "model": None, "params": {}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0,
             "model": None, "params": {}},
            {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 2000.0,
             "model": None, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }


def _rc_filter() -> dict:
    # R=1k, C=1uF: tau=1ms, fc = 1/(2*pi*RC) = 159.15 Hz. Source: 0->1V step + AC 1.
    return {
        "nodes": ["0", "IN", "OUT"],
        "devices": [
            {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 0.0, "model": None,
             "params": {"ac": {"mag": 1.0, "phase": 0.0},
                        "waveform": {"type": "PULSE", "v1": 0.0, "v2": 1.0, "td": 0.0,
                                     "tr": 1e-6, "tf": 1e-6, "pw": 1.0, "per": 2.0}}},
            {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0,
             "model": None, "params": {}},
            {"kind": "C", "name": "C1", "nodes": ["OUT", "0"], "value": 1e-6,
             "model": None, "params": {}},
        ],
        "models": {}, "params": {"TEMP": 27},
    }


def _interp_mag(x_f: list, mag: list, f: float) -> float:
    """Log-x linear interpolation of an AC magnitude trace at frequency f."""
    lx = [math.log10(v) for v in x_f]
    lf = math.log10(f)
    if lf <= lx[0]:
        return mag[0]
    if lf >= lx[-1]:
        return mag[-1]
    for i in range(len(lx) - 1):
        if lx[i] <= lf <= lx[i + 1]:
            t = (lf - lx[i]) / (lx[i + 1] - lx[i])
            return mag[i] + t * (mag[i + 1] - mag[i])
    return mag[-1]


# =============================================================================
# Gate 1 — honest degrade when ngspice is absent (forced; always runs)
# =============================================================================

def test_degrade_without_ngspice() -> None:
    old = os.environ.get("CHUDWARE_NGSPICE")
    os.environ["CHUDWARE_NGSPICE"] = "/nonexistent/ngspice-not-here"
    try:
        assert not ng.ngspice_available()
        st = ng.ngspice_status()
        assert st["installed"] is False and "dnf install ngspice" in st["install_hint"]

        res = simulate(_divider(), {"analyses": [{"type": "op"}],
                                    "probes": ["V(OUT)"]}, backend="ngspice")
        assert res["backend"] == "mna", res
        assert "not installed" in res.get("backend_note", ""), res
        assert _pct_err(res["op"]["V(OUT)"], 20.0 / 3.0) < 0.1
        # auto with no vendor attachments silently stays on mna
        res2 = simulate(_divider(), {"analyses": [{"type": "op"}],
                                     "probes": ["V(OUT)"]}, backend="auto")
        assert res2["backend"] == "mna" and "backend_note" not in res2
        print("degrade: backend='ngspice' honestly fell back to mna with a note  OK")
    finally:
        if old is None:
            os.environ.pop("CHUDWARE_NGSPICE", None)
        else:
            os.environ["CHUDWARE_NGSPICE"] = old


# =============================================================================
# Gate 2 — default backend untouched
# =============================================================================

def test_default_is_mna() -> None:
    res = simulate(_divider(), {"analyses": [{"type": "op"}], "probes": ["V(OUT)"]})
    assert res["backend"] == "mna"
    assert _pct_err(res["op"]["V(OUT)"], 20.0 / 3.0) < 0.1
    # unsupported-analysis request degrades with a note even when ngspice exists
    res2 = simulate(_divider(), {"analyses": [{"type": "mc", "runs": 3,
                                               "probe": "V(OUT)"}],
                                 "probes": ["V(OUT)"]}, backend="ngspice")
    note = res2.get("backend_note", "")
    # ngspice present -> "not supported by the ngspice backend"; absent -> "not installed"
    assert res2["backend"] == "mna" and ("not supported" in note or "not installed" in note)
    print("default backend is mna; unsupported analyses stay on mna  OK")


# =============================================================================
# Live parity gates — run only when ngspice is installed, else SKIP cleanly
# =============================================================================

def test_parity_divider() -> None:
    st = {"analyses": [{"type": "op"}], "probes": ["V(OUT)", "I(V1)"]}
    a = simulate(_divider(), st)
    b = simulate(_divider(), st, backend="ngspice")
    assert b["backend"] == "ngspice", b.get("backend_note")
    for p in ("V(OUT)", "I(V1)"):
        assert _pct_err(b["op"][p], a["op"][p]) < 0.1, (p, a["op"][p], b["op"][p])

    dc = {"analyses": [{"type": "dc", "source": "V1", "start": 0, "stop": 5,
                        "step": 0.5}], "probes": ["V(OUT)"]}
    a = simulate(_divider(), dc)
    b = simulate(_divider(), dc, backend="ngspice")
    assert b["backend"] == "ngspice"
    assert len(a["dc"]["x"]) == len(b["dc"]["x"])
    for va, vb in zip(a["dc"]["traces"]["V(OUT)"], b["dc"]["traces"]["V(OUT)"]):
        assert abs(va - vb) < 1e-3, (va, vb)
    print("parity: divider op + dc identical across backends  OK")


def test_parity_rc_filter() -> None:
    # tran: settles to 1 V; value at t=tau is 63.2%.
    tr = {"analyses": [{"type": "tran", "tstep": 1e-5, "tstop": 5e-3}],
          "probes": ["V(OUT)"]}
    a = simulate(_rc_filter(), tr)
    b = simulate(_rc_filter(), tr, backend="ngspice")
    assert b["backend"] == "ngspice", b.get("backend_note")
    fa = a["tran"]["traces"]["V(OUT)"][-1]
    fb = b["tran"]["traces"]["V(OUT)"][-1]
    assert _pct_err(fb, fa) < 1.0, (fa, fb)
    assert _pct_err(fb, 1.0 - math.exp(-5.0)) < 1.0

    # ac: unity in the passband, 1/sqrt(2) at fc, ~-20 dB/dec above.
    ac = {"analyses": [{"type": "ac", "sweep": "dec", "points": 10,
                        "fstart": 1.0, "fstop": 1e6}], "probes": ["V(OUT)"]}
    a = simulate(_rc_filter(), ac)
    b = simulate(_rc_filter(), ac, backend="ngspice")
    assert b["backend"] == "ngspice"
    fc = 1.0 / (2.0 * math.pi * 1000.0 * 1e-6)
    for res in (a, b):
        x, m = res["ac"]["x_f"], res["ac"]["mag"]["V(OUT)"]
        assert _pct_err(_interp_mag(x, m, 1.0), 1.0) < 1.0
        assert _pct_err(_interp_mag(x, m, fc), 1.0 / math.sqrt(2.0)) < 2.0
    for f in (1.0, fc, 1e4, 1e6):
        ma = _interp_mag(a["ac"]["x_f"], a["ac"]["mag"]["V(OUT)"], f)
        mb = _interp_mag(b["ac"]["x_f"], b["ac"]["mag"]["V(OUT)"], f)
        assert _pct_err(mb, ma) < 2.0, (f, ma, mb)

    # tf: gain 1 (through R into open C), rout = R.
    tf = {"analyses": [{"type": "tf", "output": "V(OUT)", "source": "V1"}],
          "probes": ["V(OUT)"]}
    a = simulate(_rc_filter(), tf)
    b = simulate(_rc_filter(), tf, backend="ngspice")
    assert b["backend"] == "ngspice"
    assert _pct_err(b["tf"]["gain"], a["tf"]["gain"]) < 0.5
    assert _pct_err(b["tf"]["rout"], a["tf"]["rout"]) < 0.5
    # rin is infinite here — both must agree it is "huge" (mna: inf, ngspice: ~1e20)
    assert a["tf"]["rin"] > 1e12 and b["tf"]["rin"] > 1e12
    print("parity: RC filter tran + ac + tf identical across backends  OK")


def test_vendor_opamp_demo() -> None:
    """Vendor .subckt attachment demo: a VCVS op-amp .lib attached to a component
    simulates through ngspice (unity buffer -> V(OUT)=V(IN)); mna warns+ignores;
    auto routes to ngspice because the circuit needs the vendor model."""
    # Vendor-.subckt enrichment (xdevices/includes + "needs backend" warnings) is the
    # ngspice leg, which STAYS Python; the C++ sim_netlist_to_circuit RPC does
    # not carry it (tracked netlist.cpp gap).
    from services.sim.spice import netlist_to_spice_circuit

    lib_text = (
        "* demo vendor op-amp (single VCVS gain stage)\n"
        ".subckt DEMO_OPAMP inp inn out\n"
        "Rin inp inn 10meg\n"
        "Eout out 0 inp inn 100k\n"
        ".ends DEMO_OPAMP\n"
    )
    with tempfile.TemporaryDirectory(prefix="chudware-ngtest-") as td:
        libp = os.path.join(td, "demo_opamp.lib")
        with open(libp, "w", encoding="utf-8") as f:
            f.write(lib_text)
        netlist = {"components": [
            {"ref": "V1", "libid": "sim:v", "value": "2.5",
             "pins": {"1": "IN", "2": "GND"},
             "sim": {"model": "V", "pin_order": ["1", "2"]}},
            # unity-follower: out fed back to inverting input
            {"ref": "U1", "libid": "vendor:opamp", "value": "",
             "pins": {"1": "IN", "2": "OUT", "3": "OUT"},
             "sim": {"model": "SUBCKT", "subckt": "DEMO_OPAMP", "lib": libp,
                     "pin_order": ["1", "2", "3"]}},
            {"ref": "R1", "libid": "sim:r", "value": "10k",
             "pins": {"1": "OUT", "2": "GND"},
             "sim": {"model": "R", "pin_order": ["1", "2"]}},
        ], "nets": []}
        circuit, warns = netlist_to_spice_circuit(netlist)
        assert circuit.get("xdevices") and circuit.get("includes") == [libp]
        assert any("needs backend='ngspice'" in w for w in warns), warns

        st = {"analyses": [{"type": "op"}], "probes": ["V(OUT)"]}
        m = simulate(circuit, st)  # default mna ignores the vendor part
        assert m["backend"] == "mna" and "vendor" in m.get("backend_note", "")
        g = simulate(circuit, st, backend="ngspice")
        assert g["backend"] == "ngspice"
        assert _pct_err(g["op"]["V(OUT)"], 2.5) < 0.1, g["op"]
        auto = simulate(circuit, st, backend="auto")
        assert auto["backend"] == "ngspice"  # vendor model forces ngspice under auto
        assert _pct_err(auto["op"]["V(OUT)"], 2.5) < 0.1
    print("vendor op-amp .subckt attachment simulates via ngspice (buffer=2.5V)  OK")


def test_parser_and_schematic_validation() -> None:
    from services.sim.spice import netlist_to_spice_circuit, parse_spice_deck

    try:
        parse_spice_deck("R1 in 0 not-a-number\nV1 in 0 1\n.op\n")
    except ValueError:
        pass
    else:
        raise AssertionError("malformed passive value was silently accepted")
    for malformed in ("R1 in 0 -1\n", "V1 in 0 PWL(0 0 1)\n"):
        try:
            parse_spice_deck(malformed)
        except ValueError:
            pass
        else:
            raise AssertionError(f"malformed deck was silently accepted: {malformed!r}")

    passive, _ = parse_spice_deck("C1 in 0 1u IC=2\nV1 in 0 1\n.op\n")
    assert passive["devices"][0]["params"]["ic"] == 2.0

    controlled, _ = parse_spice_deck(
        "V1 in 0 1\nE1 out 0 in 0 2\nF1 out2 0 V1 3\nR1 out 0 1k\n.op\n")
    mixed, _ = parse_spice_deck("Vmix in 0 AC 2 DC 1\n")
    assert mixed["devices"][0]["value"] == 1.0
    assert mixed["devices"][0]["params"]["ac"] == {"mag": 2.0, "phase": 0.0}
    assert controlled["devices"][1]["params"]["gain"] == 2.0
    assert controlled["devices"][2]["params"] == {"gain": 3.0, "ctrl": "V1"}

    circuit, warnings = netlist_to_spice_circuit({"components": [
        {"ref": "Q1", "libid": "Device:Q_NPN", "value": "",
         "pins": {"B": "NB", "C": "NC", "E": "NE"}},
        {"ref": "M1", "libid": "Device:NMOS", "value": "",
         "pins": {"G": "NG", "D": "ND", "S": "NS"}},
        {"ref": "Rbad", "libid": "Device:R", "value": "1k",
         "pins": {"1": "N1"}},
    ]})
    assert circuit["devices"][0]["nodes"] == ["NC", "NB", "NE"]
    assert circuit["devices"][1]["nodes"] == ["ND", "NG", "NS"]
    assert len(circuit["devices"]) == 2
    assert any("component skipped" in warning for warning in warnings)
    print("SPICE parser rejects malformed values; schematic pins map safely  OK")


# =============================================================================

def main() -> None:
    t0 = time.perf_counter()
    gates = [
        ("Gate 1: honest degrade without ngspice", test_degrade_without_ngspice, False),
        ("Gate 2: default backend is mna", test_default_is_mna, False),
        ("Gate 3: parser and schematic validation", test_parser_and_schematic_validation, False),
        ("Gate 4: divider parity (live)", test_parity_divider, True),
        ("Gate 5: RC filter parity (live)", test_parity_rc_filter, True),
        ("Gate 6: vendor op-amp demo (live)", test_vendor_opamp_demo, True),
    ]
    live_ok = ng.ngspice_available()
    skipped = 0
    for title, fn, needs_live in gates:
        print(f"\n--- {title} ---")
        if needs_live and not live_ok:
            print("SKIP: ngspice not installed (dnf install ngspice) — "
                  "mna-only gates still passed")
            skipped += 1
            continue
        fn()
    tail = f" ({skipped} live gate(s) skipped — ngspice absent)" if skipped else ""
    print(f"\nALL NGSPICE BACKEND GATES PASSED{tail}  "
          f"({time.perf_counter() - t0:.2f}s)")


if __name__ == "__main__":
    main()
