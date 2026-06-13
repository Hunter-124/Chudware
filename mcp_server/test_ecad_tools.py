"""AI-ECAD MCP tool tests — drive the tools as a WEAK MODEL would (house style: main()+asserts).

We register the tools against a tiny stub ``mcp`` that just collects the decorated functions (so we
exercise the real ``register_ecad_tools`` wiring without importing the stdio server), then build a
resistive divider end-to-end:

  new schematic -> find_part('resistor') returns Device:R -> place two resistors + a V source + a
  GND port -> wire them into a divider -> ecad_status() shows the right nets + unconnected pins +
  next_actions -> run_erc returns violations -> simulate('op') returns sensible node voltages
  (divider midpoint).

Every assertion checks BOTH the result AND the rich-context contract: payloads must carry ``hint``
and ``next_actions``, status must name unconnected pins, and the sim numbers must be physically
sane (divider midpoint = Vsrc * R2/(R1+R2)).

Run:  & ".../venv/Scripts/python.exe" -m mcp_server.test_ecad_tools
"""
from __future__ import annotations

import json

from mcp_server.ecad_tools import register_ecad_tools


# --------------------------------------------------------------------------- stub mcp


class _StubMCP:
    """Collects @mcp.tool()-decorated functions by name (no FastMCP needed for tests)."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _call(_tools: dict, _name: str, **kw) -> dict:
    """Invoke a tool by name and parse its JSON-string return.

    The positional params are underscore-prefixed so a tool's own ``name=`` kwarg never collides.
    """
    out = _tools[_name](**kw)
    assert isinstance(out, str), f"{_name} must return a JSON string, got {type(out)}"
    return json.loads(out)


def _assert_context(payload: dict, name: str) -> None:
    """Every payload must TEACH: hint + next_actions present (the whole point of these tools)."""
    assert "hint" in payload and payload["hint"], f"{name}: missing/empty hint"
    assert "next_actions" in payload, f"{name}: missing next_actions"
    assert isinstance(payload["next_actions"], list), f"{name}: next_actions must be a list"
    assert "warnings" in payload, f"{name}: missing warnings list"


# --------------------------------------------------------------------------- tests


def test_register() -> dict:
    mcp = _StubMCP()
    register_ecad_tools(mcp)
    expected = {
        "ecad_new_schematic", "ecad_new_pcb", "ecad_find_part", "ecad_place_symbol",
        "ecad_wire", "ecad_add_power", "ecad_label", "ecad_status", "ecad_run_erc",
        "ecad_simulate", "ecad_run_drc", "ecad_export", "ecad_to_3d",
    }
    missing = expected - set(mcp.tools)
    assert not missing, f"tools not registered: {missing}"
    print(f"  registered {len(mcp.tools)} tools: {', '.join(sorted(mcp.tools))}")
    print("  [register] PASS")
    return mcp.tools


def test_find_part(tools: dict) -> None:
    _call(tools, "ecad_new_schematic", name="divider")
    p = _call(tools, "ecad_find_part", query="resistor")
    _assert_context(p, "ecad_find_part")
    assert p["ok"] and p["hits"], "find_part returned no hits for 'resistor'"
    libids = [h.get("libid") for h in p["hits"]]
    assert "Device:R" in libids, f"Device:R not found in {libids}"
    rhit = next(h for h in p["hits"] if h.get("libid") == "Device:R")
    assert rhit["pin_count"] == 2, f"resistor should have 2 pins, got {rhit['pin_count']}"
    print(f"  find_part('resistor') -> top hits {libids[:4]}, Device:R pins={rhit['pin_count']}")
    # the source, too
    v = _call(tools, "ecad_find_part", query="voltage")
    assert any(h.get("libid") == "Simulation_SPICE:V_source" for h in v["hits"]), \
        "voltage source not findable"
    print("  [find_part] PASS")


def _build_divider(tools: dict) -> None:
    """Build a clean 10V / 1k+2k resistive divider on the active schematic.

    Layout note (the geometric care a weak model needs): symbols are spread so the auto-routed
    wires connect ONLY the intended pins (overlapping placements make L-routes cross and merge
    nets). V1 vertical at (0,20); R1/R2 vertical (angle 90) stacked at x=20. A GND power port on
    the bottom net names it 'GND' so the simulator ties it to node 0 — then V(OUT) is the
    textbook midpoint 10*2k/3k = 6.667 V.

      IN  net: V1.+  — R1.2
      OUT net: R1.1  — R2.2
      GND net: R2.1  — V1.-  (+ a GND power port -> node 0)
    """
    _call(tools, "ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=20, value="10")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=20, value="1k", angle=90)
    _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=0, value="2k", angle=90)
    _call(tools, "ecad_wire", from_ref="V1", from_pin="+", to_ref="R1", to_pin="2")
    _call(tools, "ecad_wire", from_ref="R1", from_pin="1", to_ref="R2", to_pin="2")
    _call(tools, "ecad_wire", from_ref="R2", from_pin="1", to_ref="V1", to_pin="-")
    # GND power port at V1.- world xy (0, 14.92) -> names the bottom net 'GND' -> node 0.
    _call(tools, "ecad_add_power", net="GND", x=0, y=14.92)


def test_place_and_wire(tools: dict) -> dict:
    """Build a divider: V1(10V) IN, R1 1k, R2 2k; with GND port -> V(OUT)=6.667V."""
    # Place R1 first and check the freshly-placed-pin context.
    r1 = _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=20, value="1k", angle=90)
    _assert_context(r1, "ecad_place_symbol")
    assert r1["ok"] and r1["ref"] == "R1", f"first resistor should be R1, got {r1.get('ref')}"
    assert len(r1["pins"]) == 2, "resistor must expose 2 pins"
    assert r1["unconnected_pins_this_symbol"], "freshly placed R1 pins must be unconnected"
    print(f"  placed {r1['ref']} (1k) pins={[p['number'] for p in r1['pins']]} "
          f"unconnected={r1['unconnected_pins_this_symbol']}")

    r2 = _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=0, value="2k", angle=90)
    assert r2["ref"] == "R2", f"second resistor should be R2, got {r2.get('ref')}"
    v1 = _call(tools, "ecad_place_symbol", libid="Simulation_SPICE:V_source",
               x=0, y=20, value="10")
    assert v1["ok"], "voltage source placement failed"
    print(f"  placed {v1['ref']} (10V), {r2['ref']} (2k)")

    # Wire the divider. Resistor pins are '1'/'2'; V source pins are '+'/'-'.
    w_in = _call(tools, "ecad_wire", from_ref="V1", from_pin="+", to_ref="R1", to_pin="2")
    _assert_context(w_in, "ecad_wire")
    assert w_in["ok"], f"wire V1.+->R1.2 failed: {w_in.get('error')}"
    assert w_in["net_size"] >= 2, "IN net should hold >=2 pins after wiring"
    print(f"  wired V1.+ <-> R1.2  net={w_in['net']} members={w_in['net_members']}")

    w_out = _call(tools, "ecad_wire", from_ref="R1", from_pin="1", to_ref="R2", to_pin="2")
    assert w_out["ok"], f"wire R1.1->R2.2 failed: {w_out.get('error')}"
    print(f"  wired R1.1 <-> R2.2  net={w_out['net']}")

    w_gnd = _call(tools, "ecad_wire", from_ref="R2", from_pin="1", to_ref="V1", to_pin="-")
    assert w_gnd["ok"], f"wire R2.1->V1.- failed: {w_gnd.get('error')}"
    print(f"  wired R2.1 <-> V1.-  net={w_gnd['net']} members={w_gnd['net_members']}")

    gp = _call(tools, "ecad_add_power", net="GND", x=0, y=14.92)
    _assert_context(gp, "ecad_add_power")
    assert gp["net_size"] >= 2, f"GND port should bond to >=2 pins, got {gp['net_size']}"
    print(f"  GND port bonded {gp['net_size']} pins: {gp['net_members']}")
    return tools


def test_status(tools: dict) -> None:
    st = _call(tools, "ecad_status")
    _assert_context(st, "ecad_status")
    assert st["ok"] and st["kind"] == "sch"
    assert st["component_count"] == 3, f"expected 3 components, got {st['component_count']}"
    refs = sorted(c["ref"] for c in st["components"])
    assert refs == ["R1", "R2", "V1"], f"unexpected refs {refs}"
    # Three two-pin parts wired into a divider => exactly 3 nets (IN, OUT, GND).
    assert st["net_count"] == 3, f"expected 3 nets, got {st['net_count']}: {st['nets']}"
    sizes = sorted(n["size"] for n in st["nets"])
    assert sizes == [2, 2, 2], f"each divider net should have 2 pins, got {sizes}"
    # No pin should be left unconnected once the divider is fully wired.
    assert not st["unconnected_pins"], f"unexpected unconnected pins: {st['unconnected_pins']}"
    assert "component(s)" in st["summary"]
    print(f"  status: {st['summary']}")
    print(f"          nets={[(n['name'], n['size']) for n in st['nets']]}")
    print(f"          next_actions[0]={st['next_actions'][0]!r}")
    print("  [status] PASS")


def test_unconnected_context(tools: dict) -> None:
    """A second schematic with a deliberately floating pin must name it in status + next_actions."""
    _call(tools, "ecad_new_schematic", name="floating")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, value="1k")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=0, value="2k")
    # Wire only one pin-pair, leaving the others floating.
    _call(tools, "ecad_wire", from_ref="R1", from_pin="2", to_ref="R2", to_pin="1")
    st = _call(tools, "ecad_status")
    assert st["unconnected_pins"], "expected floating pins to be reported"
    # The names must be concrete pin refs like 'R1.1'.
    assert any(u.startswith("R1.") or u.startswith("R2.") for u in st["unconnected_pins"]), \
        f"unconnected pins not named usefully: {st['unconnected_pins']}"
    # next_actions must mention wiring the unconnected pins.
    joined = " ".join(st["next_actions"]).lower()
    assert "unconnected" in joined or "wire" in joined, \
        f"next_actions should guide wiring: {st['next_actions']}"
    print(f"  floating-pin status named: {st['unconnected_pins']}")
    print("  [unconnected_context] PASS")


def test_run_erc(tools: dict) -> None:
    # A fresh, fully-wired divider schematic.
    _call(tools, "ecad_new_schematic", name="divider2")
    _build_divider(tools)

    erc = _call(tools, "ecad_run_erc")
    _assert_context(erc, "ecad_run_erc")
    assert erc["ok"], "run_erc failed"
    assert "errors" in erc and "warnings" in erc
    assert isinstance(erc["violations"], list)
    # The verdict line must read like the contract: "N error(s), M warning(s)..."
    assert "error(s)" in erc["verdict"] and "warning(s)" in erc["verdict"], erc["verdict"]
    # missing_footprint warnings are expected (no footprints assigned).
    rules = {g["rule"] for g in erc["by_rule"]}
    print(f"  erc verdict: {erc['verdict']}")
    print(f"  erc rules hit: {sorted(rules)} ({erc['errors']} err, {erc['warnings']} warn)")
    print("  [run_erc] PASS")


def test_simulate_op(tools: dict) -> None:
    """The divider on the divider2 schematic must solve to its closed-form midpoint."""
    sim = _call(tools, "ecad_simulate", analysis="op")
    _assert_context(sim, "ecad_simulate")
    assert sim["ok"], f"simulate failed: {sim.get('error')}"
    assert sim["converged"], f"op did not converge: {sim.get('sim_log')}"
    assert sim["device_count"] == 3, f"expected 3 sim devices, got {sim['device_count']}"
    op = sim["op"]
    print(f"  op node voltages: {op}")
    print(f"  reading: {sim['reading']}")
    # GND is node 0 (named via the power port), so probes cover the IN (10V) and OUT nodes.
    # Expected divider midpoint = 10 * 2k/(1k+2k) = 6.6667 V; source node = 10 V.
    vvals = {k: v for k, v in op.items() if k.startswith("V(") and v is not None}
    assert vvals, "no node voltages returned"
    in_v = max(vvals.values())   # the source node sits at ~10V
    mid = min(vvals.values())    # the divider midpoint
    assert abs(in_v - 10.0) < 0.05, f"source node should be ~10V, got {in_v}"
    assert abs(mid - 6.6667) < 0.05, \
        f"divider midpoint should be ~6.667V, got {mid} (all={vvals})"
    # The plain-English reading must mention a voltage.
    assert "V" in sim["reading"], "reading should be human-readable with units"
    print("  [simulate_op] PASS")


def test_simulate_no_devices(tools: dict) -> None:
    """Simulating an empty schematic must fail GRACEFULLY with guidance, not crash."""
    _call(tools, "ecad_new_schematic", name="empty")
    sim = _call(tools, "ecad_simulate", analysis="op")
    assert not sim["ok"], "empty schematic should not simulate"
    assert "hint" in sim and sim["next_actions"], "failure must still teach"
    print(f"  empty-sim guidance: {sim['hint']}")
    print("  [simulate_no_devices] PASS")


def test_pcb_and_drc(tools: dict) -> None:
    pcb = _call(tools, "ecad_new_pcb", name="board")
    _assert_context(pcb, "ecad_new_pcb")
    assert pcb["ok"] and pcb["copper_layers"] == 2
    drc = _call(tools, "ecad_run_drc")
    _assert_context(drc, "ecad_run_drc")
    assert drc["ok"], "run_drc failed"
    assert "verdict" in drc and "error(s)" in drc["verdict"]
    st = _call(tools, "ecad_status")
    assert st["kind"] == "pcb", "status should report the active board"
    print(f"  pcb drc verdict: {drc['verdict']}")
    print(f"  pcb status: {st['summary']}")
    print("  [pcb_and_drc] PASS")


def test_export_guard(tools: dict) -> None:
    # 3D export is best-effort; assert it returns a clean payload either way (no exception).
    out = _call(tools, "ecad_to_3d")
    assert "hint" in out and "next_actions" in out, "ecad_to_3d must always return guidance"
    print(f"  ecad_to_3d -> ok={out.get('ok')} hint={out['hint'][:60]!r}")
    print("  [export_guard] PASS")


# --------------------------------------------------------------------------- runner


def main() -> None:
    print("AI-ECAD MCP tool tests")
    tools = test_register()
    test_find_part(tools)
    test_place_and_wire(tools)
    test_status(tools)
    test_unconnected_context(tools)
    test_run_erc(tools)
    test_simulate_op(tools)
    test_simulate_no_devices(tools)
    test_pcb_and_drc(tools)
    test_export_guard(tools)
    print("\nALL ECAD TOOL TESTS PASSED")


if __name__ == "__main__":
    main()
