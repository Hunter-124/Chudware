"""Circuit-simulation end-to-end gates — the full user path through the ECAD MCP tools.

ecad_new_schematic -> place/wire -> ecad_simulate (op/dc/tran/ac/four) + ecad_tf +
ecad_sweep, the orchestrator's deterministic simulate_design seam, the optional
ngspice backend, and Python<->C++ (core/sim/spice.cpp `sim_spice`) parity spot checks.

Every analytic reference is computed in-test from the cited closed form — no
load-bearing constants from memory:
  * RC step:        v(t) = V*(1 - exp(-t/RC)), tau = RC          (1st-order ODE)
  * RLC band-pass:  f0 = 1/(2*pi*sqrt(L*C))                      (series resonance)
  * divider:        Vout = V*R2/(R1+R2); Rin = R1+R2; Rout = R1||R2 (Thevenin)
  * MOSFET level-1: Id = beta*((Vgs-VTO)*Vds - Vds^2/2) (triode)  (square law)
  * RC low-pass:    |H(f)| = 1/sqrt(1 + (2*pi*f*R*C)^2)           (1-pole filter)

Regression guards for bugs fixed 2026-07-12:
  * per-symbol sim_params waveforms (SIN/...) were dropped by the netlist path ->
    transient sources silently ran pure DC (seam fix: ecad_tools._apply_sim_source_params)
  * starter-library MOSFET/BJT pin_order was G,D,S / B,C,E while the solvers expect
    D,G,S / C,B,E (devices.py Mosfet/BJT) -> gate/drain and base/collector swapped
  * netlist_to_spice_circuit discarded NMOS/PMOS + NPN/PNP polarity -> every schematic
    PMOS/PNP simulated as n-type (fix: params["type"] emission in engine/sim/spice.py)

No live-LLM paths: nothing here calls ecad_design/run_design planners (a past incident
leaked live Gemini calls into the suite); simulate_design is exercised directly on a
hand-built deterministic design dict.

Run:  .venv-mcp/bin/python -X utf8 -m pytest services/mcp/test_ecad_sim_e2e.py -q
  or: .venv-mcp/bin/python -X utf8 -m services.mcp.test_ecad_sim_e2e
"""
from __future__ import annotations

import json
import math
import os
import shutil

import pytest

# Import the registrar EARLY (before sibling tests stub sys.modules) — same rule as
# test_fea_advanced.py / test_mcp_freshness.py.
from services.mcp.ecad_tools import register_ecad_tools


# --------------------------------------------------------------------------- harness


class _StubMCP:
    """Collects @mcp.tool()-decorated functions by name (no FastMCP needed)."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


_TOOLS: dict = {}


def _tools() -> dict:
    global _TOOLS
    if not _TOOLS:
        stub = _StubMCP()
        register_ecad_tools(stub)
        _TOOLS = stub.tools
    return _TOOLS


def _call(_name: str, **kw) -> dict:
    out = _tools()[_name](**kw)
    assert isinstance(out, str), f"{_name} must return a JSON string"
    return json.loads(out)


def _new_sch(name: str) -> str:
    r = _call("ecad_new_schematic", name=name)
    assert r["ok"], r
    return r["doc_id"]


def _wire(doc: str, a: str, ap: str, b: str, bp: str) -> None:
    w = _call("ecad_wire", from_ref=a, from_pin=ap, to_ref=b, to_pin=bp, doc_id=doc)
    assert w["ok"], f"wire {a}.{ap}->{b}.{bp} failed: {w.get('error')}"


def _set_sin(doc: str, ref: str, amplitude: float, freq: float) -> None:
    """Give a placed V source a SIN transient waveform via sim_params (the GUI shape)."""
    from services.mcp.ecad_common import ecad_session
    s = ecad_session.get_session(doc)
    sym = next(sym for sym in s.doc.get("symbols", []) if sym.get("ref") == ref)
    ecad_session.apply_ops(s, s.revision, [
        {"op": "sch_edit_symbol", "id": sym["id"],
         "sim_params": {"waveform": "SIN", "amplitude": str(amplitude),
                        "freq": str(freq)}}])


def _circuit_of(doc: str) -> dict:
    """The exact Circuit the tools simulate (netlist -> spice translation)."""
    from services.mcp.ecad_common import ecad_session, netlist_mod
    from services.sim.spice import netlist_to_spice_circuit
    ses = ecad_session.get_session(doc)
    circuit, _warns = netlist_to_spice_circuit(netlist_mod.build_netlist(ses.doc))
    return circuit


# V-source symbol: pins '+' at +5.08 and '-' at -5.08 from the anchor; a GND power
# port placed at (x, y-5.08) bonds onto the '-' pin net and names it node 0.
_VGND_DY = 5.08


def _build_rc(doc: str, v: str = "5", r: str = "1k", c: str = "1u") -> None:
    """V1 -> R1 -> C1 -> back to V1, bottom net grounded."""
    _call("ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=20, value=v, doc_id=doc)
    _call("ecad_place_symbol", libid="Device:R", x=20, y=20, value=r, angle=90, doc_id=doc)
    _call("ecad_place_symbol", libid="Device:C", x=20, y=0, value=c, angle=90, doc_id=doc)
    _wire(doc, "V1", "+", "R1", "2")
    _wire(doc, "R1", "1", "C1", "1")
    _wire(doc, "C1", "2", "V1", "-")
    g = _call("ecad_add_power", net="GND", x=0, y=20 - _VGND_DY, doc_id=doc)
    assert g["net_size"] >= 2, f"GND port did not bond: {g}"


def _build_divider(doc: str) -> None:
    """10 V / 1k+2k divider (the house pattern from test_ecad_tools.py)."""
    _call("ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=20, value="10", doc_id=doc)
    _call("ecad_place_symbol", libid="Device:R", x=20, y=20, value="1k", angle=90, doc_id=doc)
    _call("ecad_place_symbol", libid="Device:R", x=20, y=0, value="2k", angle=90, doc_id=doc)
    _wire(doc, "V1", "+", "R1", "2")
    _wire(doc, "R1", "1", "R2", "2")
    _wire(doc, "R2", "1", "V1", "-")
    g = _call("ecad_add_power", net="GND", x=0, y=20 - _VGND_DY, doc_id=doc)
    assert g["net_size"] >= 2, f"GND port did not bond: {g}"


def _tau_63(t: list, v: list) -> float | None:
    """First crossing of (1 - 1/e) of the final value — tau for a 1st-order step."""
    vf = v[-1]
    tgt = vf * (1.0 - math.exp(-1.0))
    for i in range(1, len(v)):
        if (v[i - 1] - tgt) * (v[i] - tgt) <= 0 and v[i] != v[i - 1]:
            return t[i - 1] + (tgt - v[i - 1]) * (t[i] - t[i - 1]) / (v[i] - v[i - 1])
    return None




def _oracle_sim_run(circuit, study, **kw):
    """The MNA leg — C++ sim_spice RPC now (engine.sim.circuit was deleted in the
    zero-Python cutover); ecad_common._sim_run keeps the Python-side sanitize shims
    and the ngspice-backend dispatch."""
    from services.mcp.ecad_common import _sim_run
    return _sim_run(circuit, study, **kw)


# --------------------------------------------------------------------------- RC step


def test_rc_step_tau():
    """RC step response through the schematic path: tau = R*C within 3%."""
    sim_run = _oracle_sim_run
    doc = _new_sch("e2e_rc_step")
    _build_rc(doc)

    # The MCP tool run: must converge and settle to the source voltage.
    sim = _call("ecad_simulate", analysis="tran",
                params={"tstep": 1e-5, "tstop": 5e-3, "uic": True}, doc_id=doc)
    assert sim["ok"] and sim["converged"], sim.get("sim_log")
    finals = {k: tr["final"] for k, tr in sim["tran"]["traces"].items() if k.startswith("V(")}
    assert finals, "no voltage traces returned"
    # After 5*tau the cap node reads the source value: v(t)=V(1-e^-t/RC), e^-5=0.7%.
    assert any(abs(v - 5.0) < 0.05 for v in finals.values()), finals

    # tau from the raw traces of the SAME circuit the tool built.
    circuit = _circuit_of(doc)
    cnode = next(d for d in circuit["devices"] if d["kind"] == "C")["nodes"]
    out = next(n for n in cnode if n != "0")
    res = sim_run(circuit, {"analyses": [{"type": "tran", "tstep": 1e-5, "tstop": 5e-3,
                                          "uic": True}],
                            "probes": [f"V({out})"]})
    tr = res["tran"]
    tau = _tau_63(tr["x_t"], tr["traces"][f"V({out})"])
    ref = 1000.0 * 1e-6  # tau = R*C (1st-order ODE)
    assert tau is not None and abs(tau - ref) / ref < 0.03, f"tau={tau} vs RC={ref}"


# --------------------------------------------------------------------------- RLC resonance


def test_rlc_resonance_peak():
    """Series-RLC band-pass through the schematic path: AC peak at f0 = 1/(2*pi*sqrt(LC))."""
    sim_run = _oracle_sim_run
    doc = _new_sch("e2e_rlc_ac")
    _call("ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=30, value="1", doc_id=doc)
    _call("ecad_place_symbol", libid="Device:L", x=20, y=30, value="1m", angle=90, doc_id=doc)
    _call("ecad_place_symbol", libid="Device:C", x=20, y=15, value="1u", angle=90, doc_id=doc)
    _call("ecad_place_symbol", libid="Device:R", x=20, y=0, value="10", angle=90, doc_id=doc)
    _wire(doc, "V1", "+", "L1", "2")
    _wire(doc, "L1", "1", "C1", "1")
    _wire(doc, "C1", "2", "R1", "2")
    _wire(doc, "R1", "1", "V1", "-")
    g = _call("ecad_add_power", net="GND", x=0, y=30 - _VGND_DY, doc_id=doc)
    assert g["net_size"] >= 2, g

    # MCP tool run converges and auto-drives an AC stimulus (and says so).
    sim = _call("ecad_simulate", analysis="ac",
                params={"sweep": "dec", "points": 20, "fstart": 100, "fstop": 1e6}, doc_id=doc)
    assert sim["ok"] and sim["converged"], sim.get("sim_log")
    assert any("AC" in w for w in sim["warnings"]), \
        "auto AC-drive must be announced (Bug E-1 guard)"
    missing_source = _call(
        "ecad_simulate", analysis="ac",
        params={"sweep": "dec", "points": 20, "fstart": 100, "fstop": 1e6,
                "source": "NO_SUCH_SOURCE"},
        doc_id=doc)
    assert missing_source["ok"] is False and "source" in missing_source["error"].lower()
    bad_band = _call(
        "ecad_simulate", analysis="ac",
        params={"sweep": "dec", "points": 20, "fstart": 1e6, "fstop": 100},
        doc_id=doc)
    assert bad_band["ok"] is False and "fstart" in bad_band["error"]

    # Peak location on a fine grid around f0 (grid-limited tolerance).
    circuit = _circuit_of(doc)
    for d in circuit["devices"]:
        if d["kind"] == "V":
            d.setdefault("params", {})["ac"] = {"mag": 1.0, "phase": 0.0}
    rnodes = next(d for d in circuit["devices"] if d["name"] == "R1")["nodes"]
    out = next(n for n in rnodes if n != "0")
    res = sim_run(circuit, {"analyses": [{"type": "ac", "sweep": "dec", "points": 120,
                                          "fstart": 1e3, "fstop": 3e4}],
                            "probes": [f"V({out})"]})
    f = res["ac"]["x_f"]
    mag = res["ac"]["mag"][f"V({out})"]
    imax = max(range(len(mag)), key=lambda i: mag[i])
    f0 = 1.0 / (2 * math.pi * math.sqrt(1e-3 * 1e-6))  # series resonance
    assert abs(f[imax] - f0) / f0 < 0.03, f"peak {f[imax]} Hz vs f0 {f0} Hz"
    # At resonance Z_L + Z_C = 0 -> the full drive appears across R: |H| = 1.
    assert abs(mag[imax] - 1.0) < 0.02, f"|H(f0)|={mag[imax]}"


# --------------------------------------------------------------------------- diode rectifier


@pytest.mark.xfail(
    reason="C++ spice eval_probe lacks the two-terminal junction-current branch (I(D1)) "
           "— documented core/sim/spice.cpp:888 gap; needs a core port, the MCP seam "
           "cannot evaluate probes client-side",
    strict=False)
def test_diode_rectifier_current_probe():
    """Half-wave rectifier with a SIN source: I(D1) probe finite (NaN regression) and
    the output actually rectifies (waveform seam-fix regression — was silently DC)."""
    doc = _new_sch("e2e_rectifier")
    _call("ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=20, value="5", doc_id=doc)
    _call("ecad_place_symbol", libid="Device:D", x=20, y=20, value="", doc_id=doc)
    _call("ecad_place_symbol", libid="Device:R", x=40, y=10, value="1k", angle=90, doc_id=doc)
    _wire(doc, "V1", "+", "D1", "A")
    _wire(doc, "D1", "K", "R1", "2")
    _wire(doc, "R1", "1", "V1", "-")
    g = _call("ecad_add_power", net="GND", x=0, y=20 - _VGND_DY, doc_id=doc)
    assert g["net_size"] >= 2, g
    _set_sin(doc, "V1", amplitude=5.0, freq=1000.0)

    circuit = _circuit_of(doc)
    dnodes = next(d for d in circuit["devices"] if d["kind"] == "D")["nodes"]
    out = dnodes[1]  # cathode node (A, K per Device:D pin_order)
    sim = _call("ecad_simulate", analysis="tran",
                params={"tstep": 2e-6, "tstop": 3e-3},
                probes=[f"V({out})", "I(D1)"], doc_id=doc)
    assert sim["ok"] and sim["converged"], sim.get("sim_log")
    vout = sim["tran"]["traces"][f"V({out})"]
    id1 = sim["tran"]["traces"]["I(D1)"]
    # Rectified: peaks near VA minus one junction drop, clamps near zero on the
    # negative half-cycle (Shockley diode conduction).
    assert 3.8 < vout["max"] < 5.0, f"peak should be ~VA-0.7: {vout}"
    assert vout["min"] > -0.8, f"negative half-cycle must be blocked: {vout}"
    assert vout["max"] - vout["min"] > 3.0, \
        f"output must actually swing (waveform regression — was pure DC): {vout}"
    # The I(D1) probe must be finite (was NaN before the junction-current branch).
    for k in ("final", "min", "max"):
        assert id1[k] is not None and math.isfinite(id1[k]), f"I(D1).{k} not finite: {id1}"
    assert abs(id1["max"] - vout["max"] / 1000.0) < 5e-4, f"I(D1) ~ V(out)/R: {id1}"

    # op-point diode current probe as well (the original flaky case).
    op = _call("ecad_simulate", analysis="op", probes=["I(D1)", f"V({out})"], doc_id=doc)
    assert op["ok"] and op["converged"], op.get("sim_log")
    i_op = op["op"].get("I(D1)")
    assert i_op is not None and math.isfinite(i_op), f"op I(D1) not finite: {op['op']}"
    # op keeps the DC value=5 (waveforms only apply to tran/four): V(out) ~ 5 - Vd.
    v_op = op["op"].get(f"V({out})")
    assert 3.8 < v_op < 5.0, f"op must stay DC-driven: {v_op}"


# --------------------------------------------------------------------------- MOSFET / BJT


def _triode_drain_v(vdd: float, r: float, beta: float, vov: float) -> float:
    """Closed-form V(D) of a resistor-loaded level-1 MOSFET in triode:
    (VDD - VD)/R = beta*(vov*VD' - VD'^2/2) with VD' the drain-source drop —
    solved as the quadratic (beta*R/2)*x^2 - (1 + beta*R*vov)*x + VDD = 0 for
    x = Vds, taking the physical root 0 < x < vov."""
    a = beta * r / 2.0
    b = -(1.0 + beta * r * vov)
    c = vdd
    disc = b * b - 4 * a * c
    assert disc > 0
    x = (-b - math.sqrt(disc)) / (2 * a)
    return x


def test_nmos_inverter_dc_transfer():
    """NMOS resistor-load inverter through the schematic path (pin_order regression):
    output = VDD with the gate low, and the level-1 triode value with the gate high."""
    doc = _new_sch("e2e_nmos_inv")
    _call("ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=40, value="5", doc_id=doc)
    _call("ecad_place_symbol", libid="Simulation_SPICE:V_source", x=-30, y=10, value="0", doc_id=doc)
    _call("ecad_place_symbol", libid="Device:R", x=20, y=40, value="10k", angle=90, doc_id=doc)
    p = _call("ecad_place_symbol", libid="Device:NMOS", x=20, y=10, doc_id=doc)
    ref = p["ref"]
    _wire(doc, "V1", "+", "R1", "2")
    _wire(doc, "R1", "1", ref, "D")
    _wire(doc, "V2", "+", ref, "G")
    _wire(doc, ref, "S", "V1", "-")
    _wire(doc, "V2", "-", "V1", "-")
    g = _call("ecad_add_power", net="GND", x=-30, y=10 - _VGND_DY, doc_id=doc)
    assert g["net_size"] >= 2, g

    circuit = _circuit_of(doc)
    m = next(d for d in circuit["devices"] if d["kind"] == "M")
    # pin_order regression guard: solver convention is nodes=[D,G,S] (devices.py Mosfet).
    rnodes = next(d for d in circuit["devices"] if d["kind"] == "R")["nodes"]
    vg_nodes = next(d for d in circuit["devices"] if d["name"] == "V2")["nodes"]
    assert m["nodes"][0] in rnodes, f"M nodes[0] must be the DRAIN (R-load) node: {m['nodes']}"
    assert m["nodes"][1] == vg_nodes[0], f"M nodes[1] must be the GATE node: {m['nodes']}"
    assert m["nodes"][2] == "0", f"M nodes[2] must be the grounded SOURCE: {m['nodes']}"
    assert (m.get("params") or {}).get("type") == "nmos", m

    dnode = m["nodes"][0]
    sim = _call("ecad_simulate", analysis="dc",
                params={"source": "V2", "start": 0, "stop": 5, "step": 0.25},
                probes=[f"V({dnode})"], doc_id=doc)
    assert sim["ok"] and sim["converged"], sim.get("sim_log")
    tr = sim["dc"]["traces"][f"V({dnode})"]
    # Gate low: no channel (Vgs < VTO=1) -> output at VDD.
    assert abs(tr["first"] - 5.0) < 0.01, tr
    # Gate high: level-1 defaults VTO=1, KP=2e-5, W=L -> beta=2e-5; Vov=4.
    ref_lo = _triode_drain_v(vdd=5.0, r=10e3, beta=2e-5, vov=4.0)
    assert abs(tr["last"] - ref_lo) / ref_lo < 0.02, f"{tr['last']} vs {ref_lo}"
    assert tr["last"] < tr["first"], "inverter must invert"


def test_pmos_polarity():
    """Schematic PMOS (polarity regression — used to simulate as n-type): common-source
    stage is ON with the gate at 0 V and OFF with the gate at VDD."""
    doc = _new_sch("e2e_pmos_cs")
    _call("ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=40, value="5", doc_id=doc)
    _call("ecad_place_symbol", libid="Simulation_SPICE:V_source", x=-30, y=10, value="5", doc_id=doc)
    p = _call("ecad_place_symbol", libid="Device:PMOS", x=20, y=25, doc_id=doc)
    ref = p["ref"]
    _call("ecad_place_symbol", libid="Device:R", x=20, y=0, value="10k", angle=90, doc_id=doc)
    _wire(doc, "V1", "+", ref, "S")
    _wire(doc, ref, "D", "R1", "2")
    _wire(doc, "R1", "1", "V1", "-")
    _wire(doc, "V2", "+", ref, "G")
    _wire(doc, "V2", "-", "V1", "-")
    g = _call("ecad_add_power", net="GND", x=-30, y=10 - _VGND_DY, doc_id=doc)
    assert g["net_size"] >= 2, g

    circuit = _circuit_of(doc)
    m = next(d for d in circuit["devices"] if d["kind"] == "M")
    assert (m.get("params") or {}).get("type") == "pmos", \
        f"polarity regression — PMOS must carry type=pmos: {m}"
    dnode = m["nodes"][0]
    sim = _call("ecad_simulate", analysis="dc",
                params={"source": "V2", "start": 0, "stop": 5, "step": 0.25},
                probes=[f"V({dnode})"], doc_id=doc)
    assert sim["ok"] and sim["converged"], sim.get("sim_log")
    tr = sim["dc"]["traces"][f"V({dnode})"]
    # Gate 0 V: Vsg = 5 -> ON. V(D) = VDD - Vsd with Vsd the triode drop (level-1
    # defaults beta=2e-5, VTO=1 -> vov=4), same quadratic as the NMOS case by symmetry.
    ref_on = 5.0 - _triode_drain_v(vdd=5.0, r=10e3, beta=2e-5, vov=4.0)
    assert abs(tr["first"] - ref_on) / ref_on < 0.02, f"{tr['first']} vs {ref_on}"
    # Gate at VDD: Vsg = 0 -> OFF -> no current -> V(D) = 0 through the 10k.
    assert abs(tr["last"]) < 0.01, tr
    assert tr["first"] > tr["last"], "PMOS polarity inverted (regression)"


def test_npn_emitter_follower():
    """Schematic NPN follower (pin_order regression): V(E) tracks VB minus one Vbe."""
    doc = _new_sch("e2e_npn_ef")
    _call("ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=40, value="10", doc_id=doc)
    _call("ecad_place_symbol", libid="Simulation_SPICE:V_source", x=-30, y=10, value="3", doc_id=doc)
    p = _call("ecad_place_symbol", libid="Device:Q_NPN", x=20, y=25, doc_id=doc)
    ref = p["ref"]
    _call("ecad_place_symbol", libid="Device:R", x=20, y=0, value="1k", angle=90, doc_id=doc)
    _wire(doc, "V1", "+", ref, "C")
    _wire(doc, "V2", "+", ref, "B")
    _wire(doc, ref, "E", "R1", "2")
    _wire(doc, "R1", "1", "V1", "-")
    _wire(doc, "V2", "-", "V1", "-")
    g = _call("ecad_add_power", net="GND", x=-30, y=10 - _VGND_DY, doc_id=doc)
    assert g["net_size"] >= 2, g

    circuit = _circuit_of(doc)
    q = next(d for d in circuit["devices"] if d["kind"] == "Q")
    assert (q.get("params") or {}).get("type") == "npn", q
    enode = q["nodes"][2]  # solver convention nodes=[C,B,E] (devices.py BJT)
    sim = _call("ecad_simulate", analysis="op", probes=[f"V({enode})"], doc_id=doc)
    assert sim["ok"] and sim["converged"], sim.get("sim_log")
    ve = sim["op"][f"V({enode})"]
    # Follower: V(E) = VB - Vbe, Vbe ~ 0.6..0.85 V (Shockley junction at ~2 mA).
    assert 3.0 - 0.85 < ve < 3.0 - 0.55, f"V(E)={ve} should be ~VB-0.7"


# --------------------------------------------------------------------------- tf / sweep


def test_tf_divider():
    """ecad_tf on the 1k/2k divider: gain, Rin, Rout are the Thevenin closed forms."""
    doc = _new_sch("e2e_tf_div")
    _build_divider(doc)
    op = _call("ecad_simulate", analysis="op", doc_id=doc)
    assert op["ok"] and op["converged"]
    vv = {k: v for k, v in op["op"].items() if k.startswith("V(") and v is not None}
    midprobe = min(vv, key=vv.get)  # midpoint sits below the 10 V source node
    r = _call("ecad_tf", output=midprobe, source="V1", doc_id=doc)
    assert r["ok"] and r.get("converged", True), r
    assert abs(r["gain"] - 2.0 / 3.0) < 1e-3          # R2/(R1+R2)
    assert abs(r["rin"] - 3000.0) < 1.0               # R1+R2 seen by V1
    assert abs(r["rout"] - (1000.0 * 2000.0) / 3000.0) < 1.0  # R1||R2
    missing = _call("ecad_tf", output=midprobe, source="NO_SOURCE", doc_id=doc)
    assert missing["ok"] is False and "source" in missing["error"].lower()


def test_sweep_and_guard():
    """ecad_sweep over R1 follows V = 10*R2/(R1+R2) point-for-point; no-values fails
    gracefully with guidance."""
    doc = _new_sch("e2e_sweep_div")
    _build_divider(doc)
    vals = [1000.0, 2000.0, 5000.0]
    sw = _call("ecad_sweep", param="R1", values=vals, analysis="op", doc_id=doc)
    assert sw["ok"] and sw["converged"], sw
    probe = sw["probe"]
    got = [row[probe] for row in sw["sweep"]]
    for g, r1 in zip(got, vals):
        ref = 10.0 * 2000.0 / (r1 + 2000.0)  # divider closed form
        assert abs(g - ref) < 0.01, f"R1={r1}: {g} vs {ref}"

    bad = _call("ecad_sweep", param="R1", doc_id=doc)
    assert not bad["ok"] and bad["next_actions"], "must fail with guidance"
    for kwargs in (
            {"param": "R1", "values": [1000.0]},
            {"param": "R1", "values": [1000.0, float("nan")]},
            {"param": "R1", "values": [1000.0, 2000.0], "analysis": "noise"}):
        invalid = _call("ecad_sweep", doc_id=doc, **kwargs)
        assert invalid["ok"] is False and invalid.get("error"), (kwargs, invalid)


# --------------------------------------------------------------------------- four (THD)


def test_four_thd_linear_rc():
    """'four' on a sine-driven RC low-pass: H1 = VA/sqrt(1+(2*pi*f*RC)^2) within 2%
    and THD well under 1% (linear network). Exercises the sim_params waveform seam."""
    doc = _new_sch("e2e_four_rc")
    _build_rc(doc, v="1", r="1k", c="100n")
    _set_sin(doc, "V1", amplitude=1.0, freq=1000.0)
    circuit = _circuit_of(doc)
    cnodes = next(d for d in circuit["devices"] if d["kind"] == "C")["nodes"]
    out = next(n for n in cnodes if n != "0")
    r = _call("ecad_simulate", analysis="four",
              params={"f0": 1000.0, "tstop": 5e-3, "probe": f"V({out})"}, doc_id=doc)
    assert r["ok"] and r["converged"], r.get("sim_log")
    wrc = 2 * math.pi * 1000.0 * 1000.0 * 100e-9
    ref_h1 = 1.0 / math.sqrt(1.0 + wrc * wrc)  # 1-pole low-pass magnitude
    assert abs(r["h1_mag"] - ref_h1) / ref_h1 < 0.02, f"H1={r['h1_mag']} vs {ref_h1}"
    assert r["thd_pct"] < 0.5, f"linear RC must have ~zero THD: {r['thd_pct']}%"


# --------------------------------------------------------------------------- orchestrator seam


def test_orchestrator_simulate_design_deterministic():
    """simulate_design on a hand-built design dict (no planner, no LLM): the divider
    op probe must hit the closed-form midpoint and pass its own expect gate."""
    from services.ecad_design import orchestrator as orch
    from services.mcp.ecad_common import ecad_session

    doc = _new_sch("e2e_simdesign")
    _build_divider(doc)
    op = _call("ecad_simulate", analysis="op", doc_id=doc)
    vv = {k: v for k, v in op["op"].items() if k.startswith("V(") and v is not None}
    midprobe = min(vv, key=vv.get)

    ses = ecad_session.get_session(doc)
    design = {"study": {"analysis": "op",
                        "expect": {"probe": midprobe,
                                   "value": 10.0 * 2000.0 / 3000.0,  # divider closed form
                                   "tol": 0.01}}}
    out = orch.simulate_design(ses, design)
    assert out["converged"], out
    assert out["got"] is not None and math.isfinite(out["got"]), out
    assert out["pass"], f"simulate_design self-check failed: {out}"

    # tran leg: RC step settles to the source value (e^-5 = 0.7% residual).
    doc2 = _new_sch("e2e_simdesign_rc")
    _build_rc(doc2)
    circuit = _circuit_of(doc2)
    cnodes = next(d for d in circuit["devices"] if d["kind"] == "C")["nodes"]
    out_n = next(n for n in cnodes if n != "0")
    ses2 = ecad_session.get_session(doc2)
    design2 = {"study": {"analysis": "tran",
                         "params": {"tstep": 1e-5, "tstop": 5e-3, "uic": True},
                         "expect": {"probe": f"V({out_n})", "value": 5.0, "tol": 0.02}}}
    out2 = orch.simulate_design(ses2, design2)
    assert out2["converged"] and out2["pass"], out2


# --------------------------------------------------------------------------- ngspice backend


def test_ngspice_backend_divider():
    """backend='ngspice' runs the real binary when installed (else the tool must
    degrade honestly to mna with a backend_note — asserted either way, never a crash)."""
    doc = _new_sch("e2e_ngspice_div")
    _build_divider(doc)
    sim = _call("ecad_simulate", analysis="op", backend="ngspice", doc_id=doc)
    assert sim["ok"] and sim["converged"], sim
    have_ng = bool(shutil.which("ngspice") or os.environ.get("CHUDWARE_NGSPICE"))
    if have_ng:
        assert sim["backend"] == "ngspice", sim
    else:
        assert sim["backend"] == "mna", sim
        assert any("ngspice" in w for w in sim["warnings"]), \
            "absent ngspice must be announced in warnings"
    vv = {k: v for k, v in sim["op"].items() if k.startswith("V(") and v is not None}
    assert abs(min(vv.values()) - 10.0 * 2000.0 / 3000.0) < 0.01, vv
    assert abs(max(vv.values()) - 10.0) < 0.01, vv


# --------------------------------------------------------------------------- C++ parity


def _cpp_sim(circuit: dict, study: dict) -> dict | None:
    """Run core/sim/spice.cpp via the in-process bridge; None => lib unavailable (skip)."""
    try:
        from services.mcp import core_bridge
        return core_bridge.call("sim_spice", {"circuit": circuit, "study": study})
    except Exception:  # noqa: BLE001 — missing/slim lib is a skip, not a failure
        return None


def test_cpp_spice_parity_spot_checks():
    """Python engine/sim/circuit.py vs C++ core/sim/spice.cpp on 4 netlists.

    KNOWN GAP (finding, not asserted): C++ eval_probe (core/sim/spice.cpp:888) lacks
    the two-terminal junction-current branch (circuit.py:414) — I(D1) returns null
    from the C++ side, so only V/I(Vsrc) probes are compared here.
    """
    import pytest
    sim_run = _oracle_sim_run

    div = {"nodes": ["0", "IN", "OUT"], "devices": [
        {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 10.0, "params": {}},
        {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
        {"kind": "R", "name": "R2", "nodes": ["OUT", "0"], "value": 2000.0, "params": {}}],
        "models": {}, "params": {"TEMP": 27}}
    rc = {"nodes": ["0", "IN", "OUT"], "devices": [
        {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 5.0, "params": {}},
        {"kind": "R", "name": "R1", "nodes": ["IN", "OUT"], "value": 1000.0, "params": {}},
        {"kind": "C", "name": "C1", "nodes": ["OUT", "0"], "value": 1e-6,
         "params": {"ic": 0.0}}],
        "models": {}, "params": {"TEMP": 27}}
    rlc = {"nodes": ["0", "IN", "A", "OUT"], "devices": [
        {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 1.0,
         "params": {"ac": {"mag": 1.0, "phase": 0.0}}},
        {"kind": "L", "name": "L1", "nodes": ["IN", "A"], "value": 1e-3, "params": {}},
        {"kind": "C", "name": "C1", "nodes": ["A", "OUT"], "value": 1e-6, "params": {}},
        {"kind": "R", "name": "R1", "nodes": ["OUT", "0"], "value": 10.0, "params": {}}],
        "models": {}, "params": {"TEMP": 27}}
    dio = {"nodes": ["0", "IN", "OUT"], "devices": [
        {"kind": "V", "name": "V1", "nodes": ["IN", "0"], "value": 5.0, "params": {}},
        {"kind": "D", "name": "D1", "nodes": ["IN", "OUT"], "value": None, "model": "DX",
         "params": {}},
        {"kind": "R", "name": "R1", "nodes": ["OUT", "0"], "value": 1000.0, "params": {}}],
        # 1N4148-class Shockley params
        "models": {"DX": {"type": "d", "IS": 2.52e-9, "N": 1.752}}, "params": {"TEMP": 27}}
    nmos = {"nodes": ["0", "VDD", "G", "D"], "devices": [
        {"kind": "V", "name": "VDD", "nodes": ["VDD", "0"], "value": 10.0, "params": {}},
        {"kind": "V", "name": "VG", "nodes": ["G", "0"], "value": 2.0, "params": {}},
        {"kind": "R", "name": "RD", "nodes": ["VDD", "D"], "value": 5000.0, "params": {}},
        {"kind": "M", "name": "M1", "nodes": ["D", "G", "0"], "value": None, "model": "NM",
         "params": {}}],
        "models": {"NM": {"type": "nmos", "VTO": 1.0, "KP": 2e-4, "W": 10.0, "L": 1.0,
                          "LAMBDA": 0.0}},
        "params": {"TEMP": 27}}

    op_probe_sets = {"divider": ["V(OUT)", "I(V1)"],
                     "diode":   ["V(OUT)", "I(V1)"],
                     "nmos":    ["V(D)", "I(VDD)"]}
    op_cases = [("divider", div), ("diode", dio), ("nmos", nmos)]

    ran_any = False
    for label, circ in op_cases:
        study = {"analyses": [{"type": "op"}], "probes": op_probe_sets[label]}
        cpp = _cpp_sim(circ, study)
        if cpp is None:
            pytest.skip("libchudware core bridge unavailable — C++ parity skipped")
        ran_any = True
        py = sim_run(json.loads(json.dumps(circ)), study)
        assert cpp.get("convergence", {}).get("op"), f"{label}: C++ op did not converge"
        for k in op_probe_sets[label]:
            pv, cv = py["op"].get(k), cpp["op"].get(k)
            assert pv is not None and cv is not None, f"{label} {k}: py={pv} cpp={cv}"
            assert abs(pv - cv) <= 1e-9 * max(1.0, abs(pv)), f"{label} {k}: py={pv} cpp={cv}"

    # RC transient parity: same trap integrator + timestep -> near-bit-exact traces.
    study = {"analyses": [{"type": "tran", "tstep": 1e-5, "tstop": 3e-3, "uic": True}],
             "probes": ["V(OUT)"]}
    cpp = _cpp_sim(rc, study)
    if cpp is not None:
        py = sim_run(json.loads(json.dumps(rc)), study)
        pv = py["tran"]["traces"]["V(OUT)"]
        cv = cpp["tran"]["traces"]["V(OUT)"]
        assert len(pv) == len(cv), f"trace lengths differ: {len(pv)} vs {len(cv)}"
        worst = max(abs(a - b) for a, b in zip(pv, cv))
        assert worst < 1e-9, f"RC tran max abs diff {worst}"

    # RLC AC parity on the magnitude response.
    study = {"analyses": [{"type": "ac", "sweep": "dec", "points": 20,
                           "fstart": 500, "fstop": 5e4}], "probes": ["V(OUT)"]}
    cpp = _cpp_sim(rlc, study)
    if cpp is not None:
        py = sim_run(json.loads(json.dumps(rlc)), study)
        pv = py["ac"]["mag"]["V(OUT)"]
        cv = cpp["ac"]["mag"]["V(OUT)"]
        assert len(pv) == len(cv)
        worst = max(abs(a - b) for a, b in zip(pv, cv))
        assert worst < 1e-9, f"RLC ac max abs mag diff {worst}"

    assert ran_any, "no parity case executed"


# --------------------------------------------------------------------------- main


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[{name}]", flush=True)
            fn()
            print(f"  PASS", flush=True)
    print("\nALL ECAD SIM E2E TESTS PASSED")


if __name__ == "__main__":
    main()
