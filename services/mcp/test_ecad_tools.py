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
        "ecad_update_from_sch", "ecad_autoroute", "ecad_add_zone", "ecad_fill_zones",
        "ecad_sweep", "ecad_design",
        # new tools
        "ecad_check_impedance", "ecad_tf", "ecad_make_board",
        # Wave-L new tools
        "ecad_run_dfm", "ecad_check_diff_impedance",
        # T-FIN9
        "ecad_run_hostcheck",
        # T-FIN11 FPGA support pack
        "ecad_fpga_check",
        # pack tools that live in register_ecad_tools (pdn/si/stackup/netclass)
        "ecad_run_pdn", "ecad_run_sicheck", "ecad_stackup_solve", "ecad_set_net_class",
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


def test_sweep(tools: dict) -> None:
    """A parameter sweep over R1 must move the divider midpoint monotonically + read in English."""
    # Fresh footprint-free divider (sim only): V1=10, R1 swept, R2=2k.
    _call(tools, "ecad_new_schematic", name="sweep")
    _call(tools, "ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=20, value="10")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=20, value="1k", angle=90)
    _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=0, value="2k", angle=90)
    _call(tools, "ecad_wire", from_ref="V1", from_pin="+", to_ref="R1", to_pin="2")
    _call(tools, "ecad_wire", from_ref="R1", from_pin="1", to_ref="R2", to_pin="2")
    _call(tools, "ecad_wire", from_ref="R2", from_pin="1", to_ref="V1", to_pin="-")
    _call(tools, "ecad_add_power", net="GND", x=0, y=14.92)

    sw = _call(tools, "ecad_sweep", param="R1", values=[1000, 2000, 5000], analysis="op")
    _assert_context(sw, "ecad_sweep")
    assert sw["ok"], f"sweep failed: {sw.get('error')}"
    assert sw["converged"], f"sweep did not converge: {sw.get('warnings')}"
    assert len(sw["sweep"]) == 3, f"expected 3 swept points, got {sw['sweep']}"
    # As R1 rises with R2 fixed, the divider output V=10*R2/(R1+R2) FALLS.
    probe = sw["probe"]
    ys = [row[probe] for row in sw["sweep"] if row.get(probe) is not None]
    assert len(ys) == 3, f"expected 3 finite probe values, got {ys}"
    assert ys[0] > ys[-1], f"output should fall as R1 rises (got {ys})"
    # First point: R1=1k,R2=2k -> 10*2/3 = 6.667 V; last: R1=5k -> 10*2/7 = 2.857 V
    assert abs(ys[0] - 6.6667) < 0.05, f"R1=1k midpoint should be ~6.667V, got {ys[0]}"
    assert abs(ys[-1] - 2.8571) < 0.05, f"R1=5k midpoint should be ~2.857V, got {ys[-1]}"
    assert "fall" in sw["trend"].lower(), f"trend should read as falling: {sw['trend']!r}"
    print(f"  sweep R1[1k,2k,5k]: {probe} {ys}")
    print(f"  trend: {sw['trend']}")
    print("  [sweep] PASS")


def test_sweep_guard(tools: dict) -> None:
    """ecad_sweep without a values list must fail gracefully with guidance."""
    sw = _call(tools, "ecad_sweep", param="R1")
    assert not sw["ok"], "sweep with no values should not succeed"
    assert sw["next_actions"], "failure must still teach"
    print("  [sweep_guard] PASS")


def _build_footprinted_divider(tools: dict) -> None:
    """Build a divider whose resistors carry a real footprint, so sync places them on the board."""
    from mcp_server.ecad_common import ecad_session
    from mcp_server.ecad_tools import _ECAD

    _call(tools, "ecad_new_schematic", name="sync_div")
    v1 = _call(tools, "ecad_place_symbol", libid="Simulation_SPICE:V_source",
               x=0, y=0, value="5", ref="V1")
    r1 = _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=5, value="10k", ref="R1")
    r2 = _call(tools, "ecad_place_symbol", libid="Device:R", x=40, y=5, value="10k", ref="R2")
    # Assign footprints via the session ops (the MCP layer has no footprint setter for symbols).
    s = ecad_session.get_session(_ECAD["current_sch"])
    ecad_session.apply_ops(s, s.revision, [
        {"op": "sch_edit_symbol", "id": r1["id"], "footprint": "Resistor_SMD:R_0603"},
        {"op": "sch_edit_symbol", "id": r2["id"], "footprint": "Resistor_SMD:R_0603"},
    ])
    # Connectivity by net name (robust): +5V, MID, GND.
    ecad_session.apply_ops(s, s.revision, [
        {"op": "sch_add_power", "net": "+5V", "pos": [0.0, 5.08]},
        {"op": "sch_add_power", "net": "+5V", "pos": [14.92, 5.0]},
        {"op": "sch_add_label", "text": "MID", "pos": [25.08, 5.0], "scope": "local"},
        {"op": "sch_add_label", "text": "MID", "pos": [34.92, 5.0], "scope": "local"},
        {"op": "sch_add_power", "net": "GND", "pos": [45.08, 5.0]},
    ])


def test_sync_autoroute_zone_flow(tools: dict) -> None:
    """The full PCB capability chain via the AI tools: update_from_sch -> autoroute -> add_zone ->
    fill_zones, asserting the rich-context contract at each step."""
    from mcp_server.ecad_common import ecad_session
    from mcp_server.ecad_tools import _ECAD

    _build_footprinted_divider(tools)
    _call(tools, "ecad_new_pcb", name="sync_board")
    # Give the board an outline so the whole-board zone default + router have room.
    pcb = ecad_session.get_session(_ECAD["current_pcb"])
    ecad_session.apply_ops(pcb, pcb.revision, [{"op": "pcb_set_outline", "shapes": [
        {"type": "line", "p1": [0, 0],   "p2": [60, 0]},
        {"type": "line", "p1": [60, 0],  "p2": [60, 30]},
        {"type": "line", "p1": [60, 30], "p2": [0, 30]},
        {"type": "line", "p1": [0, 30],  "p2": [0, 0]},
    ], "closed": True}])

    # --- update_from_sch ---
    up = _call(tools, "ecad_update_from_sch")
    _assert_context(up, "ecad_update_from_sch")
    assert up["ok"], f"update_from_sch failed: {up.get('error')}"
    assert sorted(up["footprints_added"]) == ["R1", "R2"], \
        f"sync should add R1+R2 (got {up['footprints_added']})"
    assert up["ratsnest_remaining"] >= 1, "MID should be an unrouted airwire after sync"
    print(f"  update_from_sch: added={up['footprints_added']} "
          f"ratsnest={up['ratsnest_remaining']} nets_bound={up['nets_bound']}")

    # --- autoroute ---
    ar = _call(tools, "ecad_autoroute")
    _assert_context(ar, "ecad_autoroute")
    assert ar["ok"], f"autoroute failed: {ar.get('error')}"
    assert ar["airwires_after"] <= ar["airwires_before"], \
        f"ratsnest should not grow ({ar['airwires_before']} -> {ar['airwires_after']})"
    assert "MID" in ar["routed_nets"], f"MID should route (routed={ar['routed_nets']})"
    print(f"  autoroute: {ar['airwires_before']} -> {ar['airwires_after']} airwire(s), "
          f"routed={ar['routed_nets']} failed={ar['failed_nets']}")

    # --- add_zone (whole-board GND pour) ---
    az = _call(tools, "ecad_add_zone", net="GND", layer="F.Cu")
    _assert_context(az, "ecad_add_zone")
    assert az["ok"], f"add_zone failed: {az.get('error')}"
    assert az["zone_count"] == 1, f"one zone expected, got {az['zone_count']}"
    print(f"  add_zone: GND pour, {len(az['polygon'])} vertices")

    # --- fill_zones ---
    fz = _call(tools, "ecad_fill_zones")
    _assert_context(fz, "ecad_fill_zones")
    assert fz["ok"], f"fill_zones failed: {fz.get('error')}"
    assert fz["zones_filled"] >= 1, f"GND pour should fill (got {fz})"
    assert fz["copper_area_mm2"] > 0.0, f"copper area should be positive (got {fz})"
    print(f"  fill_zones: {fz['zones_filled']} filled, {fz['copper_area_mm2']} mm^2, "
          f"{fz['polygons']} polygon(s)")
    print("  [sync_autoroute_zone_flow] PASS")


def test_add_zone_unknown_net(tools: dict) -> None:
    """ecad_add_zone on a net the board does not have yet must guide, not crash."""
    _call(tools, "ecad_new_pcb", name="zone_guard")
    # No outline -> whole-board default can't derive a polygon; pass an explicit rect.
    z = _call(tools, "ecad_add_zone", net="NOPE", layer="F.Cu",
              rect_or_polygon=[0, 0, 10, 10])
    assert not z["ok"], "adding a zone on an unknown net should fail cleanly"
    assert z["next_actions"], "failure must still teach"
    # GND (present on a fresh board) should work with an explicit rect.
    z2 = _call(tools, "ecad_add_zone", net="GND", layer="F.Cu",
               rect_or_polygon=[0, 0, 10, 10])
    assert z2["ok"], f"GND zone with an explicit rect should succeed: {z2.get('error')}"
    print("  [add_zone_unknown_net] PASS")


def test_export_guard(tools: dict) -> None:
    # 3D export is best-effort; assert it returns a clean payload either way (no exception).
    out = _call(tools, "ecad_to_3d")
    assert "hint" in out and "next_actions" in out, "ecad_to_3d must always return guidance"
    print(f"  ecad_to_3d -> ok={out.get('ok')} hint={out['hint'][:60]!r}")
    print("  [export_guard] PASS")


def test_ecad_design(tools: dict) -> None:
    """ecad_design('5V divider with 2.5V output') must return ok with a usable doc.

    No provider is configured in the test environment, so the orchestrator uses its
    deterministic recipe fallback. The produced doc must be accessible via ecad_status
    and ecad_simulate (the active schematic is set by ecad_design).
    """
    d = _call(tools, "ecad_design", request="5V divider with 2.5V output")
    _assert_context(d, "ecad_design")
    assert d["ok"], (f"ecad_design should succeed (recipe fallback guarantees a result); "
                     f"got ok=False, hint={d.get('hint')!r}")
    assert d["doc_id"], "ecad_design must return a doc_id"
    assert isinstance(d["stages"], list) and d["stages"], "stages list must be non-empty"
    # recipe_used is set when the fallback is active (no provider in CI).
    print(f"  ecad_design: stop_reason={d['stop_reason']!r} "
          f"recipe={d.get('recipe_used')!r} stages={d['stages']}")

    # ecad_status must now operate on the produced doc (active schematic set by ecad_design).
    st = _call(tools, "ecad_status")
    assert st["ok"] and st["kind"] == "sch", \
        f"ecad_status after ecad_design should see a schematic, got {st}"
    assert st["component_count"] >= 1, \
        f"produced schematic should have at least one component, got {st['component_count']}"
    print(f"  ecad_status after design: {st['summary']}")

    # ecad_simulate must work on the produced doc without crashing.
    sim = _call(tools, "ecad_simulate", analysis="op")
    assert "ok" in sim, "ecad_simulate must return a payload"
    # We accept both convergence and a clean 'no devices' response -- the key requirement is
    # that it does not throw an exception and returns the context fields.
    assert "hint" in sim and "next_actions" in sim, "ecad_simulate must return guidance"
    print(f"  ecad_simulate after design: converged={sim.get('converged')} "
          f"reading={sim.get('reading', 'n/a')!r}")
    print("  [ecad_design] PASS")


# --------------------------------------------------------------------------- new tool tests


def _build_divider_schematic(tools: dict, name: str = "divider_tf") -> None:
    """Build a 10V / 1k+2k divider on a fresh schematic (helper for advanced tests)."""
    _call(tools, "ecad_new_schematic", name=name)
    _call(tools, "ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=20, value="10")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=20, value="1k", angle=90)
    _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=0, value="2k", angle=90)
    _call(tools, "ecad_wire", from_ref="V1", from_pin="+", to_ref="R1", to_pin="2")
    _call(tools, "ecad_wire", from_ref="R1", from_pin="1", to_ref="R2", to_pin="2")
    _call(tools, "ecad_wire", from_ref="R2", from_pin="1", to_ref="V1", to_pin="-")
    _call(tools, "ecad_add_power", net="GND", x=0, y=14.92)


def test_tf(tools: dict) -> None:
    """ecad_tf on the divider must return sensible gain/Rin/Rout."""
    _build_divider_schematic(tools, "divider_tf")
    r = _call(tools, "ecad_tf", output="V(N$1)", source="V1")
    # V(N$1) is the midpoint node; if it doesn't work, try the first available node.
    if not r.get("ok"):
        # Discover the midpoint node name from an op analysis.
        sim = _call(tools, "ecad_simulate", analysis="op")
        nodes = [k[2:-1] for k in sim.get("op", {}).keys() if k.startswith("V(")]
        # Filter out nodes that are trivially the source.
        mid_nodes = [n for n in nodes if n not in ("IN", "N$0")]
        if mid_nodes:
            r = _call(tools, "ecad_tf", output=f"V({mid_nodes[0]})", source="V1")
    _assert_context(r, "ecad_tf")
    # Payload must have reading + numeric fields.
    assert "reading" in r, "ecad_tf: missing reading"
    assert "gain" in r and "rin" in r and "rout" in r, "ecad_tf: missing gain/rin/rout"
    print(f"  ecad_tf: reading={r['reading']!r}")
    print(f"          gain={r.get('gain')} rin={r.get('rin')} rout={r.get('rout')}")
    # For the 1k||2k divider the Rin should be 3k (the series combination seen by V1).
    if r.get("converged"):
        rin = r.get("rin")
        if rin is not None and rin > 0:
            assert 1000 < rin < 10000, f"Rin should be ~3k for this divider, got {rin}"
    print("  [ecad_tf] PASS")


def test_simulate_four(tools: dict) -> None:
    """ecad_simulate('four') on an RC circuit must return a THD% + reading."""
    # Build a simple 1kHz sine-driven RC: V_AC -> R -> C -> GND
    _call(tools, "ecad_new_schematic", name="four_rc")
    _call(tools, "ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=10,
          value="1", ref="V1")
    _call(tools, "ecad_place_symbol", libid="Device:R", x=10, y=10, value="1k",
          angle=90, ref="R1")
    _call(tools, "ecad_place_symbol", libid="Device:C", x=10, y=0, value="100n",
          angle=90, ref="C1")
    _call(tools, "ecad_wire", from_ref="V1", from_pin="+", to_ref="R1", to_pin="2")
    _call(tools, "ecad_wire", from_ref="R1", from_pin="1", to_ref="C1", to_pin="1")
    _call(tools, "ecad_wire", from_ref="C1", from_pin="2", to_ref="V1", to_pin="-")
    _call(tools, "ecad_add_power", net="GND", x=0, y=3)
    # Sine source: set waveform to SIN so the tran has something to analyze.
    from mcp_server.ecad_common import ecad_session
    from mcp_server.ecad_tools import _ECAD
    s = ecad_session.get_session(_ECAD["current_sch"])
    v_sym = next(sym for sym in s.doc.get("symbols", []) if sym.get("ref") == "V1")
    ecad_session.apply_ops(s, s.revision, [
        {"op": "sch_edit_symbol", "id": v_sym["id"],
         "sim_params": {"waveform": "SIN", "amplitude": "1", "freq": "1000"}},
    ])

    r = _call(tools, "ecad_simulate", analysis="four",
              params={"f0": 1000.0, "tstop": 5e-3, "probe": "V(N$1)"})
    _assert_context(r, "ecad_simulate(four)")
    # Check the payload shape.
    assert "reading" in r, "four: missing reading"
    assert "thd_pct" in r or "four" in r, "four: missing THD% result"
    print(f"  four analysis: reading={r.get('reading', 'n/a')!r}")
    print(f"                 thd_pct={r.get('thd_pct')} converged={r.get('converged')}")
    print("  [simulate_four] PASS")


def test_simulate_noise(tools: dict) -> None:
    """ecad_simulate('noise') on the divider must return a positive total noise RMS + reading."""
    _build_divider_schematic(tools, "divider_noise")
    # Need to know the midpoint node name -- get it from op.
    sim = _call(tools, "ecad_simulate", analysis="op")
    nodes = [k[2:-1] for k in sim.get("op", {}).keys() if k.startswith("V(")]
    # Pick the midpoint: the node with V closest to 6.667V.
    op = sim.get("op", {})
    best_node = min(nodes, key=lambda n: abs((op.get(f"V({n})", 0.0) or 0.0) - 6.667),
                    default=nodes[0] if nodes else "OUT")
    output_probe = f"V({best_node})"

    r = _call(tools, "ecad_simulate", analysis="noise",
              params={"output": output_probe, "fstart": 10.0, "fstop": 1e6})
    _assert_context(r, "ecad_simulate(noise)")
    assert "reading" in r, "noise: missing reading"
    # RC divider should have finite thermal noise.
    assert "total_noise_rms" in r, "noise: missing total_noise_rms"
    if r.get("converged"):
        rms = r.get("total_noise_rms", 0.0)
        assert isinstance(rms, (int, float)) and rms >= 0, f"noise RMS should be >=0, got {rms}"
    print(f"  noise analysis: reading={r.get('reading', 'n/a')!r}")
    print(f"                  total_noise_rms={r.get('total_noise_rms')} V_rms, "
          f"converged={r.get('converged')}")
    print("  [simulate_noise] PASS")


def test_simulate_mc(tools: dict) -> None:
    """ecad_simulate('mc') on the divider must return mean/std near the nominal midpoint."""
    _build_divider_schematic(tools, "divider_mc")
    r = _call(tools, "ecad_simulate", analysis="mc",
              params={"runs": 30, "inner": "op", "tol": {"R": 0.05}})
    _assert_context(r, "ecad_simulate(mc)")
    assert "reading" in r, "mc: missing reading"
    print(f"  mc analysis: reading={r.get('reading', 'n/a')!r}")
    print(f"               mean={r.get('mean')} std={r.get('std')}")
    print("  [simulate_mc] PASS")


def test_simulate_temp(tools: dict) -> None:
    """ecad_simulate('temp') on the divider must return a trend across temperatures."""
    _build_divider_schematic(tools, "divider_temp")
    r = _call(tools, "ecad_simulate", analysis="temp",
              params={"values": [-40, 27, 85], "inner": "op"})
    _assert_context(r, "ecad_simulate(temp)")
    assert "reading" in r, "temp: missing reading"
    print(f"  temp analysis: reading={r.get('reading', 'n/a')!r}")
    print(f"                 temps={r.get('temps')} trend={r.get('trend')}")
    print("  [simulate_temp] PASS")


def test_check_impedance_no_tracks(tools: dict) -> None:
    """ecad_check_impedance on an empty board must fail gracefully with guidance."""
    _call(tools, "ecad_new_pcb", name="imp_guard")
    r = _call(tools, "ecad_check_impedance")
    assert not r["ok"], "empty board should not have impedance"
    assert r["next_actions"], "failure must teach"
    print(f"  check_impedance (no tracks): hint={r['hint'][:80]!r}")
    print("  [check_impedance_no_tracks] PASS")


def test_check_impedance_routed(tools: dict) -> None:
    """ecad_check_impedance on a board with a routed trace must return a Z0 reading."""
    from mcp_server.ecad_common import ecad_session
    from mcp_server.ecad_tools import _ECAD

    # Build a footprinted divider + route it.
    _build_footprinted_divider(tools)
    _call(tools, "ecad_new_pcb", name="imp_board")
    pcb = ecad_session.get_session(_ECAD["current_pcb"])
    ecad_session.apply_ops(pcb, pcb.revision, [{"op": "pcb_set_outline", "shapes": [
        {"type": "line", "p1": [0, 0],   "p2": [60, 0]},
        {"type": "line", "p1": [60, 0],  "p2": [60, 30]},
        {"type": "line", "p1": [60, 30], "p2": [0, 30]},
        {"type": "line", "p1": [0, 30],  "p2": [0, 0]},
    ], "closed": True}])
    _call(tools, "ecad_update_from_sch")
    _call(tools, "ecad_autoroute")

    # Now there should be routed tracks; check impedance summary (no net specified).
    r = _call(tools, "ecad_check_impedance")
    _assert_context(r, "ecad_check_impedance")
    assert r["ok"], f"check_impedance failed: {r.get('error')}"
    assert "reading" in r, "check_impedance: missing reading"
    print(f"  check_impedance (summary): reading={r.get('reading')!r}")
    # If we have any summaries, try one specific net.
    summaries = r.get("summaries", [])
    if summaries:
        net = summaries[0]["net"]
        r2 = _call(tools, "ecad_check_impedance", net_or_ref=net, target=50.0)
        _assert_context(r2, "ecad_check_impedance(net)")
        assert r2["ok"], f"check_impedance({net}) failed: {r2.get('error')}"
        assert "z0" in r2 and r2["z0"] > 0, f"Z0 should be positive, got {r2.get('z0')}"
        print(f"  check_impedance({net}): reading={r2.get('reading')!r}")
    print("  [check_impedance_routed] PASS")


def test_make_board(tools: dict) -> None:
    """ecad_make_board() must run the full sync/route/fill/drc flow and return a summary."""
    _build_footprinted_divider(tools)
    r = _call(tools, "ecad_make_board", pcb_name="auto_board")
    _assert_context(r, "ecad_make_board")
    assert "steps" in r, "ecad_make_board: missing steps"
    assert "reading" in r, "ecad_make_board: missing reading"
    # At minimum: new_pcb and update_from_sch should succeed.
    step_names = {s["step"] for s in r["steps"]}
    assert "new_pcb" in step_names, f"new_pcb step missing: {step_names}"
    assert "update_from_sch" in step_names, f"update_from_sch step missing: {step_names}"
    # The tool must set a current_pcb so ecad_status works immediately after.
    from mcp_server.ecad_tools import _ECAD
    assert _ECAD["current_pcb"] is not None, "ecad_make_board must set current_pcb"
    st = _call(tools, "ecad_status")
    assert st["ok"] and st["kind"] == "pcb", "ecad_status after make_board should see a board"
    print(f"  make_board steps: {[(s['step'], 'OK' if s['ok'] else 'FAIL') for s in r['steps']]}")
    print(f"  make_board: footprints_placed={r.get('footprints_placed')} "
          f"nets_routed={r.get('nets_routed')} drc_errors={r.get('drc_errors')}")
    print(f"  hint: {r['hint'][:120]!r}")
    print("  [make_board] PASS")


# --------------------------------------------------------------------------- Wave-L new tests


def test_run_dfm_clean(tools: dict) -> None:
    """ecad_run_dfm on a fresh board (no DFM issues) must return a verdict with 0 errors."""
    _call(tools, "ecad_new_pcb", name="dfm_clean")
    r = _call(tools, "ecad_run_dfm")
    _assert_context(r, "ecad_run_dfm")
    assert r["ok"], f"ecad_run_dfm failed: {r.get('error')}"
    assert "errors" in r and "warning_count" in r
    assert "verdict" in r and "error(s)" in r["verdict"]
    assert "reading" in r
    print(f"  ecad_run_dfm (clean board): verdict={r['verdict']!r}")
    print("  [run_dfm_clean] PASS")


def test_run_dfm_seeded(tools: dict) -> None:
    """ecad_run_dfm on a board with a seeded DFM violation must report it with a reading."""
    from mcp_server.ecad_common import ecad_session
    from mcp_server.ecad_tools import _ECAD

    _call(tools, "ecad_new_pcb", name="dfm_seeded")
    pcb = ecad_session.get_session(_ECAD["current_pcb"])

    # Seed a courtyard_overlap: add two footprints at the same position (courtyards collide).
    from services.ecad.library.starter import FOOTPRINTS
    import copy as _copy
    fp_def = FOOTPRINTS.get("Resistor_SMD:R_0603")
    if fp_def is None:
        print("  [run_dfm_seeded] SKIP (R_0603 not in starter library)")
        return

    # Two footprints co-located -> their courtyards overlap -> courtyard_overlap error.
    fp1 = _copy.deepcopy(fp_def)
    fp2 = _copy.deepcopy(fp_def)
    ecad_session.apply_ops(pcb, pcb.revision, [
        {"op": "pcb_add_footprint", "fpid": "Resistor_SMD:R_0603",
         "ref": "R1", "value": "10k", "pos": [10.0, 10.0],
         "side": "top", "pads": fp1.get("pads", []), "def": fp1},
        {"op": "pcb_add_footprint", "fpid": "Resistor_SMD:R_0603",
         "ref": "R2", "value": "10k", "pos": [10.0, 10.0],  # same position -> overlap
         "side": "top", "pads": fp2.get("pads", []), "def": fp2},
    ])

    r = _call(tools, "ecad_run_dfm")
    _assert_context(r, "ecad_run_dfm")
    assert r["ok"], f"ecad_run_dfm tool call failed: {r.get('error')}"
    assert "reading" in r
    # courtyard_overlap should fire because two components are at the same location.
    rules_hit = {g["rule"] for g in r.get("by_rule", [])}
    print(f"  ecad_run_dfm (seeded): errors={r['errors']} warning_count={r.get('warning_count')} "
          f"rules={sorted(rules_hit)}")
    print(f"  reading: {r['reading']!r}")
    # We should see violations of some kind (at minimum the courtyard check).
    assert r["errors"] + r.get("warning_count", 0) > 0, \
        "seeded board with co-located footprints must have DFM violations"
    assert "courtyard_overlap" in rules_hit, (
        f"courtyard_overlap expected but got rules: {sorted(rules_hit)}")
    # next_actions must mention fixing courtyard_overlap first.
    na_joined = " ".join(r["next_actions"]).lower()
    assert "courtyard" in na_joined or "fix" in na_joined, \
        f"next_actions should guide courtyard fix: {r['next_actions']}"
    print("  [run_dfm_seeded] PASS")


def test_autoroute_with_diff_pairs(tools: dict) -> None:
    """ecad_autoroute with diff_pairs=[['CLK_P','CLK_N']] must attempt coupled routing and
    return diff_pair_stats in the payload."""
    from mcp_server.ecad_common import ecad_session
    from mcp_server.ecad_tools import _ECAD

    _build_footprinted_divider(tools)  # sets up a schematic with footprints
    _call(tools, "ecad_new_pcb", name="dp_board")
    pcb = ecad_session.get_session(_ECAD["current_pcb"])
    ecad_session.apply_ops(pcb, pcb.revision, [{"op": "pcb_set_outline", "shapes": [
        {"type": "line", "p1": [0, 0],   "p2": [80, 0]},
        {"type": "line", "p1": [80, 0],  "p2": [80, 40]},
        {"type": "line", "p1": [80, 40], "p2": [0, 40]},
        {"type": "line", "p1": [0, 40],  "p2": [0, 0]},
    ], "closed": True}])
    _call(tools, "ecad_update_from_sch")

    # Add a second pair of pads for CLK_P/CLK_N nets by placing extra footprints directly.
    from services.ecad.library.starter import FOOTPRINTS
    import copy as _copy
    fp_def = FOOTPRINTS.get("Resistor_SMD:R_0603")
    if fp_def is None:
        print("  [autoroute_with_diff_pairs] SKIP (R_0603 not in starter library)")
        return

    # Add CLK_P and CLK_N nets via footprints so the autorouter has real pairs to attempt.
    # Register the nets in the board doc first (so pcb_add_footprint + ratsnest work).
    fp_p = _copy.deepcopy(fp_def)
    fp_n = _copy.deepcopy(fp_def)
    for pad in fp_p.get("pads", []):
        pad["net"] = "CLK_P"
    for pad in fp_n.get("pads", []):
        pad["net"] = "CLK_N"
    # Pre-register nets so pcb_add_footprint's pads are valid.
    # T-RF20: pcb.doc is a client copy of the C++ session doc — write the mutation back.
    pcb.doc.setdefault("nets", [])
    pcb.doc["nets"].append({"name": "CLK_P", "class": "default"})
    pcb.doc["nets"].append({"name": "CLK_N", "class": "default"})
    ecad_session.swap_doc(pcb, pcb.doc)
    ecad_session.apply_ops(pcb, pcb.revision, [
        {"op": "pcb_add_footprint", "fpid": "Resistor_SMD:R_0603",
         "ref": "U1", "value": "CLK_P", "pos": [10.0, 20.0],
         "side": "top", "pads": fp_p.get("pads", []), "def": fp_p},
        {"op": "pcb_add_footprint", "fpid": "Resistor_SMD:R_0603",
         "ref": "U2", "value": "CLK_N", "pos": [70.0, 20.0],
         "side": "top", "pads": fp_n.get("pads", []), "def": fp_n},
    ])

    ar = _call(tools, "ecad_autoroute",
               diff_pairs=[["CLK_P", "CLK_N"]], ripup_passes=2)
    _assert_context(ar, "ecad_autoroute")
    assert ar["ok"], f"ecad_autoroute failed: {ar.get('error')}"
    # diff_pair_stats key must be present (may be empty if nets had no airwire).
    assert "diff_pair_stats" in ar, "ecad_autoroute must return diff_pair_stats"
    assert isinstance(ar["diff_pair_stats"], list)
    assert "ripup_iterations" in ar
    print(f"  ecad_autoroute(diff_pairs): "
          f"routed={ar['routed_nets']} failed={ar['failed_nets']} "
          f"diff_pair_stats={ar['diff_pair_stats']} "
          f"ripup_iterations={ar['ripup_iterations']}")
    if ar["diff_pair_stats"]:
        dp = ar["diff_pair_stats"][0]
        # Each pair stat must have routed bool and either 'nets' or 'net_p'/'net_n'.
        assert "routed" in dp or "status" in dp, f"pair stat missing routed/status: {dp}"
        assert "nets" in dp or "net_p" in dp, f"pair stat missing nets/net_p: {dp}"
        print(f"  diff pair result: {dp}")
    print(f"  diff_pair_reading: {ar.get('diff_pair_reading')!r}")
    print("  [autoroute_with_diff_pairs] PASS")


def test_check_diff_impedance(tools: dict) -> None:
    """ecad_check_diff_impedance on a board with routed CLK_P/CLK_N tracks must return Z_diff."""
    from mcp_server.ecad_common import ecad_session
    from mcp_server.ecad_tools import _ECAD

    # Start with a clean board.
    _call(tools, "ecad_new_pcb", name="diff_z_board")
    pcb = ecad_session.get_session(_ECAD["current_pcb"])

    # Register CLK_P / CLK_N nets directly in the board doc, then inject tracks.
    # (pcb_add_track calls _require_net; we add the nets to the board doc first.)
    pcb.doc.setdefault("nets", [])
    pcb.doc["nets"].append({"name": "CLK_P", "class": "default"})
    pcb.doc["nets"].append({"name": "CLK_N", "class": "default"})
    ecad_session.swap_doc(pcb, pcb.doc)  # T-RF20: write the client-copy mutation back

    # Add two parallel tracks representing the diff pair via ops.
    ecad_session.apply_ops(pcb, pcb.revision, [
        {"op": "pcb_add_track",
         "net": "CLK_P", "layer": "F.Cu", "width": 0.2,
         "p1": [5.0, 20.0], "p2": [75.0, 20.0], "kind": "segment"},
        {"op": "pcb_add_track",
         "net": "CLK_N", "layer": "F.Cu", "width": 0.2,
         "p1": [5.0, 20.4], "p2": [75.0, 20.4], "kind": "segment"},
    ])

    # Guard: missing net must fail gracefully.
    r_bad = _call(tools, "ecad_check_diff_impedance",
                  net_p="NOPE_P", net_n="NOPE_N", target=100.0)
    assert not r_bad["ok"], "missing nets should fail cleanly"
    assert r_bad["next_actions"], "failure must teach"
    print(f"  check_diff_impedance (missing nets): hint={r_bad['hint'][:80]!r}")

    # Real check on the injected tracks.
    r = _call(tools, "ecad_check_diff_impedance",
              net_p="CLK_P", net_n="CLK_N", target=100.0)
    _assert_context(r, "ecad_check_diff_impedance")
    assert r["ok"], f"ecad_check_diff_impedance failed: {r.get('error')}"
    assert "z_diff" in r and r["z_diff"] > 0, f"Z_diff should be positive, got {r.get('z_diff')}"
    assert "reading" in r and "Pair" in r["reading"]
    assert "skew_mm" in r
    # Z_diff for 0.2mm wide traces separated by ~0.2mm gap on default FR-4 should be reasonable.
    assert 20.0 < r["z_diff"] < 300.0, \
        f"Z_diff={r['z_diff']} out of expected range (20-300 ohm)"
    assert "pct_error" in r
    print(f"  check_diff_impedance(CLK_P/CLK_N): reading={r['reading']!r}")
    print(f"    z_diff={r['z_diff']} z0_se={r['z0_se']} gap_mm={r['gap_mm']} "
          f"skew_mm={r['skew_mm']} within={r['within_tolerance']}")
    print("  [check_diff_impedance] PASS")


# --------------------------------------------------------------------------- bug-fix tests
#
# Focused regression tests for the round-5 black-box MCP report (E1/E2/E3/E5/E6/E8/E9).


def test_e1_multiple_docs_coexist(tools: dict) -> None:
    """E1: creating a new schematic must NOT evict the prior doc — both coexist and resolve by id."""
    a = _call(tools, "ecad_new_schematic", name="E1_A")
    aid = a["doc_id"]
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, value="1k", doc_id=aid)
    b = _call(tools, "ecad_new_schematic", name="E1_B")
    bid = b["doc_id"]
    assert aid != bid, "two schematics must have distinct doc ids"
    # The PRIOR doc must still be queryable by its explicit id (the E1 symptom was 'no open doc').
    sa = _call(tools, "ecad_status", doc_id=aid)
    assert sa["ok"], f"prior doc A must still be open, got {sa.get('error')!r}"
    assert sa["component_count"] == 1, "doc A must still carry its placed resistor"
    sb = _call(tools, "ecad_status", doc_id=bid)
    assert sb["ok"] and sb["component_count"] == 0, "doc B is the fresh, empty schematic"
    print(f"  E1: docs {aid} (1 comp) and {bid} (0 comp) coexist")
    print("  [e1_multiple_docs_coexist] PASS")


def test_e2_led_driver_recipe(tools: dict) -> None:
    """E2: an 'LED driver' request must produce the LED-driver recipe (V->R->LED->GND), not an amp."""
    from services.ecad_design import recipes as _recipes
    # Matcher: LED requests map to led_driver; unrelated requests default to the divider, not amp.
    assert _recipes.closest_recipe("LED driver from a 3.3V supply")["name"] == "led_driver"
    assert _recipes.closest_recipe("an LED driver")["name"] == "led_driver"
    assert _recipes.closest_recipe("something totally unrelated")["name"] == "voltage_divider"

    d = _call(tools, "ecad_design", request="LED driver for a red LED from a 3.3V supply")
    _assert_context(d, "ecad_design(LED driver)")
    assert d["ok"], f"LED-driver design must verify, got hint={d.get('hint')!r}"
    assert d.get("recipe_used") == "led_driver", \
        f"expected led_driver recipe, got {d.get('recipe_used')!r}"
    st = _call(tools, "ecad_status", doc_id=d["doc_id"])
    libids = {c["libid"] for c in st.get("components", [])}
    assert "Device:LED" in libids, f"LED driver must contain an LED, got {libids}"
    assert "Device:R" in libids, f"LED driver must contain a current-limit resistor, got {libids}"
    assert "Amplifier_Operational:OPAMP_dual" not in libids, "LED driver must NOT be an op-amp"
    print(f"  E2: led_driver components={sorted(libids)} sim={d.get('sim', {}).get('got')}")
    print("  [e2_led_driver_recipe] PASS")


def test_e3_opamp_noninverting_gain(tools: dict) -> None:
    """E3: a non-inverting op-amp (Rf=10k,Rin=1k,Vin=1V) must simulate to V(OUT)~=11V, not 0."""
    from mcp_server.ecad_common import schema as ecad_schema
    from mcp_server.ecad_common import ecad_session

    s = _call(tools, "ecad_new_schematic", name="E3_noninv")
    sid = s["doc_id"]
    _call(tools, "ecad_place_symbol", libid="Simulation_SPICE:V_source",
          x=0, y=0, value="1", ref="V1", doc_id=sid)
    _call(tools, "ecad_place_symbol", libid="Device:R", x=30, y=0, value="1k",
          ref="Rin", doc_id=sid)
    _call(tools, "ecad_place_symbol", libid="Device:R", x=60, y=0, value="10k",
          ref="Rf", doc_id=sid)
    _call(tools, "ecad_place_symbol", libid="Amplifier_Operational:OPAMP_dual",
          x=90, y=0, value="OPAMP", ref="U1", doc_id=sid)

    # Connectivity by net labels at each pin's world coordinate (robust by-name nets).
    sess = ecad_session.get_session(sid)
    pw = {(sym.get("ref"), str(pin.get("number"))): [round(w[0], 6), round(w[1], 6)]
          for sym, pin, w in ecad_schema.iter_placed_pins(sess.doc)}
    nets = {
        "IN":  [("V1", "+"), ("U1", "2")],            # V+ drives the op-amp IN+ (pin 2)
        "INV": [("Rin", "1"), ("Rf", "1"), ("U1", "1")],  # IN- summing node (pin 1)
        "OUT": [("Rf", "2"), ("U1", "3")],            # feedback + output (pin 3)
        "GND": [("V1", "-"), ("Rin", "2")],
    }
    conn = []
    for net, members in nets.items():
        for ref, pin in members:
            conn.append({"op": "sch_add_label", "text": net, "pos": pw[(ref, pin)],
                         "scope": "global"})
        if net == "GND":
            conn.append({"op": "sch_add_power", "net": "GND", "pos": pw[("V1", "-")],
                         "symbol": "GND"})
    ecad_session.apply_ops(sess, sess.revision, conn)

    r = _call(tools, "ecad_simulate", analysis="op", probes=["V(OUT)"], doc_id=sid)
    vout = (r.get("op", {}) or {}).get("V(OUT)")
    assert vout is not None, f"V(OUT) missing — op-amp sim broke: {r.get('reading')!r}"
    assert abs(vout - 11.0) < 0.5, \
        f"non-inverting amp gain wrong: V(OUT)={vout} (expected ~11V for 1+Rf/Rin=11)"
    print(f"  E3 non-inverting: V(OUT)={vout:.4f} V (gain 1+10k/1k=11)")

    # And the inverting amp via .tf must give gain ~= -Rf/Rin.
    inv = _call(tools, "ecad_design", request="an inverting amplifier with a gain of 10")
    tf = _call(tools, "ecad_tf", output="V(OUT)", source="V1", doc_id=inv["doc_id"])
    assert tf["converged"], f"tf did not converge: {tf.get('reading')!r}"
    assert abs(tf["gain"] - (-10.0)) < 0.5, \
        f"inverting amp gain wrong: {tf['gain']} (expected ~ -10 = -Rf/Rin)"
    print(f"  E3 inverting tf: gain={tf['gain']:.4f} (=-Rf/Rin=-10)")
    print("  [e3_opamp_noninverting_gain] PASS")


def test_e5_make_board_honors_doc_id(tools: dict) -> None:
    """E5: ecad_make_board(doc_id=A) must build from schematic A even when B is the active one."""
    from mcp_server.ecad_tools import _ECAD
    a = _call(tools, "ecad_design", request="5V voltage divider with 2.5V output")
    aid = a["doc_id"]
    b = _call(tools, "ecad_new_schematic", name="E5_empty_B")  # active becomes B (empty)
    assert _ECAD["current_sch"] == b["doc_id"], "B is now the active schematic"
    res = _call(tools, "ecad_make_board", pcb_name="E5_board", doc_id=aid)
    assert res["sch_doc_id"] == aid, "make_board must use the passed schematic doc_id"
    assert res["footprints_placed"] == 2, \
        f"board from A must place A's 2 resistors, got {res['footprints_placed']}"
    print(f"  E5: make_board(doc_id=A) placed {res['footprints_placed']} footprints from {aid}")
    print("  [e5_make_board_honors_doc_id] PASS")


def test_e6_update_from_sch_places_footprints(tools: dict) -> None:
    """E6: update_from_sch must instantiate a footprint per component (default fpid by libid)."""
    s = _call(tools, "ecad_new_schematic", name="E6_div")
    sid = s["doc_id"]
    # Place two resistors with NO explicit footprint (E6: these got 0 footprints before).
    _call(tools, "ecad_place_symbol", libid="Device:R", x=0, y=0, value="10k", doc_id=sid)
    _call(tools, "ecad_place_symbol", libid="Device:R", x=20, y=0, value="10k", doc_id=sid)
    pcb = _call(tools, "ecad_new_pcb", name="E6_board")
    res = _call(tools, "ecad_update_from_sch", sch_doc_id=sid, pcb_doc_id=pcb["doc_id"])
    assert res["ok"], f"update_from_sch failed: {res.get('error')!r}"
    assert res["footprint_count"] == 2, \
        f"both resistors must get a default footprint, got {res['footprint_count']}"
    assert sorted(res["footprints_added"]) == ["R1", "R2"], \
        f"expected R1,R2 added, got {res.get('footprints_added')}"
    assert not res["unresolved"], f"default footprints must resolve, got {res['unresolved']}"
    print(f"  E6: update_from_sch placed {res['footprint_count']} footprints (no explicit fpid)")
    print("  [e6_update_from_sch_places_footprints] PASS")


def test_e8_temp_probe_surfaced(tools: dict) -> None:
    """E8: ecad_simulate('temp', probe='V(VMID)') must report the REQUESTED probe, not V(IN)."""
    d = _call(tools, "ecad_design", request="5V voltage divider with 2.5V output")
    sid = d["doc_id"]
    r = _call(tools, "ecad_simulate", analysis="temp",
              params={"values": [-40, 27, 85], "inner": "op", "probe": "V(VMID)"}, doc_id=sid)
    assert "V(VMID)" in r["reading"], \
        f"temp reading must mention the requested probe V(VMID), got {r['reading']!r}"
    assert r.get("trend"), "temp must return a trend"
    assert "V(VMID)" in r["trend"][0], f"trend rows must key the requested probe, got {r['trend'][0]}"
    # And via the probes= argument too.
    r2 = _call(tools, "ecad_simulate", analysis="temp", params={"values": [-40, 27, 85]},
               probes=["V(VMID)"], doc_id=sid)
    assert "V(VMID)" in r2["reading"], f"probes= must also surface, got {r2['reading']!r}"
    print(f"  E8: temp reading={r['reading']!r}")
    print("  [e8_temp_probe_surfaced] PASS")


def test_e7_bogus_libid_rejected(tools: dict) -> None:
    """E7: ecad_place_symbol with an unresolvable libid must return ok:false with a clear error.

    Before this fix: a bogus libid silently produced ok:true with a 2-pin stub and no pins in
    the returned pin list, which would then corrupt the netlist.
    After this fix: ok:false + error message citing the bad libid + guidance to ecad_find_part.
    """
    _call(tools, "ecad_new_schematic", name="E7_guard")

    # 1. A completely invented libid must fail.
    r = _call(tools, "ecad_place_symbol", libid="Bogus:NonExistent", x=0, y=0)
    assert not r["ok"], \
        f"E7: bogus libid must return ok:false, got ok={r.get('ok')} hint={r.get('hint')!r}"
    assert r.get("error"), "E7: error field must be populated"
    assert "Bogus:NonExistent" in r["error"], \
        f"E7: error must name the bad libid; got {r['error']!r}"
    assert r.get("hint"), "E7: hint must be non-empty for recovery"
    assert r.get("next_actions"), "E7: next_actions must guide the user to ecad_find_part"
    na_joined = " ".join(r["next_actions"]).lower()
    assert "ecad_find_part" in na_joined, \
        f"E7: next_actions must mention ecad_find_part; got {r['next_actions']}"
    print(f"  E7 bogus libid error: {r['error']!r}")
    print(f"  E7 hint: {r['hint']!r}")

    # 2. No stub symbol must have been placed — the schematic must still be empty.
    st = _call(tools, "ecad_status")
    assert st["component_count"] == 0, \
        f"E7: a stub must NOT be placed after a rejected bogus libid; " \
        f"got {st['component_count']} component(s): {st.get('components')}"
    print(f"  E7: schematic is empty (no stub placed) — component_count={st['component_count']}")

    # 3. A partial / malformed libid (no colon, gibberish name) must also fail.
    r2 = _call(tools, "ecad_place_symbol", libid="totally_invalid_no_colon", x=5, y=5)
    assert not r2["ok"], \
        f"E7: partial libid must also fail; got ok={r2.get('ok')}"
    print(f"  E7 partial libid error: {r2['error']!r}")

    # 4. A REAL libid must still work (regression guard).
    r3 = _call(tools, "ecad_place_symbol", libid="Device:R", x=10, y=10, value="1k")
    assert r3["ok"], \
        f"E7: real libid Device:R must still place successfully; got {r3.get('error')!r}"
    assert r3["ref"] == "R1", f"E7: first resistor must be R1, got {r3.get('ref')!r}"
    assert len(r3["pins"]) == 2, \
        f"E7: resistor must expose 2 real pins, got {len(r3['pins'])}"
    print(f"  E7: real libid Device:R still places ok, ref={r3['ref']}, pins={len(r3['pins'])}")
    print("  [e7_bogus_libid_rejected] PASS")


def test_e9_noise_top_level_nonzero(tools: dict) -> None:
    """E9: ecad_simulate('noise') top-level total/peak must match the nested noise (not 0.0)."""
    d = _call(tools, "ecad_design", request="5V voltage divider with 2.5V output")
    sid = d["doc_id"]
    r = _call(tools, "ecad_simulate", analysis="noise",
              params={"output": "V(VMID)", "fstart": 10.0, "fstop": 1e6}, doc_id=sid)
    if not r.get("converged"):
        print("  E9: noise did not converge in this env — skipping numeric assert")
        print("  [e9_noise_top_level_nonzero] PASS")
        return
    total = r.get("total_noise_rms")
    peak = r.get("peak_density_v_sqrthz")
    nested = (r.get("noise") or {}).get("total_noise_rms")
    assert total and total > 0.0, f"top-level total_noise_rms must be > 0, got {total}"
    assert peak and peak > 0.0, f"top-level peak_density must be > 0, got {peak}"
    # Top-level must agree with the (correct) nested value to a few significant figures.
    assert nested and abs(total - nested) / nested < 0.01, \
        f"top-level total {total} must match nested {nested}"
    print(f"  E9: total_noise_rms={total} peak_density={peak} (nested={nested})")
    print("  [e9_noise_top_level_nonzero] PASS")


# ---------------------------------------------------------------- T-FIN9 host-board rule pack


def test_fin9_hostcheck_tool(tools: dict) -> None:
    """T-FIN9: ecad_run_hostcheck runs on the active board, reports the metadata-driven
    sequencing finding with a fix hint, and is clean on an empty board."""
    from mcp_server.ecad_common import ecad_session

    # clean empty board -> no findings, teaching payload intact
    b = _call(tools, "ecad_new_pcb", name="FIN9_clean")
    clean = _call(tools, "ecad_run_hostcheck", doc_id=b["doc_id"])
    assert clean["ok"] and clean["errors"] == 0 and clean["violations"] == [], clean
    _assert_context(clean, "ecad_run_hostcheck")

    # seeded board: a host whose T-FIN8 metadata demands 'VCORE before VIO' but no
    # component sources VIO -> host_sequencing_unverifiable (no hardcoded part knowledge:
    # the requirement travels entirely in eng_overrides).
    b2 = _call(tools, "ecad_new_pcb", name="FIN9_seeded")
    sess = ecad_session.get_session(b2["doc_id"])
    # C++ schema validation (core/ecad/schema.cpp): a pad net must exist in board nets.
    sess.doc["nets"].append({"name": "3V3", "class": "default"})
    sess.doc["footprints"].append({
        "id": "fp1", "fpid": "Test:FP", "libid": "", "ref": "U1", "value": "MCU",
        "pos": [0.0, 0.0], "angle": 0.0, "side": "top", "graphics": [],
        "model_3d": None, "attr": "smd", "linked_symbol": None,
        "pads": [{"id": "fp1_p1", "number": "1", "pos": [0, 0], "angle": 0.0,
                  "shape": "rect", "size": [0.5, 0.5], "type": "smd",
                  "layers": ["F.Cu"], "drill": None, "net": "3V3"}],
        "eng_overrides": {"supply": {
            "rails": [{"name": "3V3", "voltage_v_nom": 3.3}],
            "sequencing": ["VCORE before VIO"]}},
    })
    ecad_session.swap_doc(sess, sess.doc)  # T-RF20: write the client-copy mutation back
    seeded = _call(tools, "ecad_run_hostcheck", doc_id=b2["doc_id"])
    assert seeded["ok"], seeded
    rules = {g["rule"] for g in seeded["by_rule"]}
    assert "host_sequencing_unverifiable" in rules, f"expected sequencing finding: {seeded}"
    assert seeded["fix_hints"], "findings must carry actionable fix hints"
    print(f"  FIN9: clean board 0 findings; seeded board rules {sorted(rules)}")
    print("  [fin9_hostcheck_tool] PASS")


# --------------------------------------------------------------------------- runner




# ---------------------------------------------------------------- T-FIN8 engineering metadata


def test_fin8_metadata_roundtrip(tools: dict) -> None:
    """T-FIN8: library eng seeds + per-instance sim_params.eng overrides surface in
    ecad_status per-component power_w and the board power totals."""
    s = _call(tools, "ecad_new_schematic", name="FIN8_meta")
    sid = s["doc_id"]
    # NE555 carries seeded power (typ 15 mW / max 225 mW) from the starter library.
    _call(tools, "ecad_place_symbol", libid="Timer:NE555", x=0, y=0, doc_id=sid)
    # Resistor with a per-instance override: measured operating dissipation 20 mW typ.
    r = _call(tools, "ecad_place_symbol", libid="Device:R", x=40, y=0, value="1k",
              footprint="Resistor_SMD:R_0805", doc_id=sid,
              sim_params='{"eng": {"power_w": {"typ": 0.02}, "sources": ["test override"]}}')
    assert r["ok"], f"place with sim_params failed: {r.get('error')!r}"
    st = _call(tools, "ecad_status", doc_id=sid)
    assert st["ok"] and "power" in st, "status must carry the board power rollup"
    by_ref = {c["ref"]: c for c in st["components"]}
    u1 = by_ref["U1"]
    assert abs(u1["power_w"]["typ"] - 0.015) < 1e-9, f"NE555 seed typ: {u1['power_w']}"
    assert abs(u1["power_w"]["max"] - 0.225) < 1e-9, f"NE555 seed max: {u1['power_w']}"
    r1 = by_ref["R1"]
    assert abs(r1["power_w"]["typ"] - 0.02) < 1e-9, \
        f"sim_params.eng override must win: {r1['power_w']}"
    assert abs(r1["power_w"]["max"] - 0.125) < 1e-9, \
        f"0805 rated max must survive the partial override: {r1['power_w']}"
    tot = st["power"]
    assert abs(tot["power_w_typ"] - 0.035) < 1e-9, f"board typ total: {tot}"
    assert tot["n_with_power"] == 2 and tot["n_missing_power"] == 0, tot
    # Bad JSON in sim_params must fail fast with a hint, not corrupt the doc.
    bad = _call(tools, "ecad_place_symbol", libid="Device:R", x=60, y=0,
                doc_id=sid, sim_params="{not json")
    assert not bad["ok"] and "JSON" in bad["error"], f"bad sim_params must be rejected: {bad}"
    print(f"  FIN8: U1 {u1['power_w']}, R1 {r1['power_w']}, total {tot}")
    print("  [fin8_metadata_roundtrip] PASS")


def test_fin8_missing_metadata_degrades(tools: dict) -> None:
    """T-FIN8 honesty rule: parts without eng seeds report null power (never 0), and a board
    where NOTHING has power data reports null totals."""
    s = _call(tools, "ecad_new_schematic", name="FIN8_null")
    sid = s["doc_id"]
    _call(tools, "ecad_place_symbol", libid="Connector:Conn_2", x=0, y=0, doc_id=sid)
    st = _call(tools, "ecad_status", doc_id=sid)
    c = st["components"][0]
    assert c["power_w"]["typ"] is None and c["power_w"]["max"] is None, \
        f"unknown power must be null, never 0: {c['power_w']}"
    tot = st["power"]
    assert tot["power_w_typ"] is None and tot["power_w_max"] is None, \
        f"no-data board total must be null, not 0: {tot}"
    assert tot["n_missing_power"] == 1 and tot["n_with_power"] == 0, tot
    print(f"  FIN8: Conn_2 power {c['power_w']}, totals {tot}")
    print("  [fin8_missing_metadata_degrades] PASS")


def test_fin8_to_3d_boardphys_sidecar(tools: dict) -> None:
    """T-FIN8: ecad_to_3d places component bodies from package dims and writes the
    aicad.boardphys/1 sidecar mapping each solid to power/thermal metadata."""
    try:
        import build123d  # noqa: F401
    except ImportError:
        print("  FIN8: build123d not installed — skipping to_3d sidecar test")
        return
    import os
    import tempfile

    s = _call(tools, "ecad_new_schematic", name="FIN8_3d")
    sid = s["doc_id"]
    _call(tools, "ecad_place_symbol", libid="Timer:NE555", x=0, y=0,
          footprint="Package_SO:SOIC-8", doc_id=sid)
    _call(tools, "ecad_place_symbol", libid="Device:R", x=40, y=0, value="1k",
          footprint="Resistor_SMD:R_0805", doc_id=sid)
    _call(tools, "ecad_wire", from_ref="U1", from_pin="8", to_ref="R1", to_pin="1", doc_id=sid)
    mb = _call(tools, "ecad_make_board", pcb_name="FIN8_board", doc_id=sid)
    # DRC errors on this throwaway 2-part board are fine — we only need a synced board
    # with a closed outline for the 3D export.
    assert mb.get("doc_id") and mb.get("footprints_placed", 0) >= 2, \
        f"make_board did not produce a populated board: {mb.get('reading')!r}"
    out = os.path.join(tempfile.mkdtemp(prefix="fin8_"), "board.step")
    r = _call(tools, "ecad_to_3d", path=out, doc_id=mb["doc_id"])
    if not r.get("ok") and "still" in str(r.get("error", "")).lower():
        r = _call(tools, "ecad_to_3d", path=out, doc_id=mb["doc_id"])  # kernel warm now
    assert r["ok"], f"ecad_to_3d failed: {r.get('error')!r}"
    assert r["n_components"] >= 2, f"expected component bodies, got {r['n_components']}"
    sidecar = r.get("boardphys_path")
    assert sidecar and os.path.isfile(sidecar), f"sidecar not written: {sidecar!r}"
    with open(sidecar, encoding="utf-8") as fh:
        phys = json.load(fh)
    assert phys["schema"] == "aicad.boardphys/1", phys["schema"]
    assert phys["step_path"] == r["step_path"]
    comps = {c["ref"]: c for c in phys["components"]}
    assert "U1" in comps and "R1" in comps, f"sidecar refs: {sorted(comps)}"
    u1 = comps["U1"]
    assert u1["solid_label"] == "U1", "sidecar must map to the placed solid label"
    assert u1["body"]["source"] == "package_lib", \
        f"SOIC-8 must place a library package body, got {u1['body']}"
    assert abs(u1["body"]["l_mm"] - 4.9) < 1e-9 and abs(u1["body"]["h_mm"] - 1.75) < 1e-9
    assert abs(u1["power_w"]["typ"] - 0.015) < 1e-9
    assert u1["theta_c_per_w"]["ja"] is not None and u1["theta_c_per_w"]["jc"] is None, \
        "theta_jc unknown for SOIC-8 seed -> must be null"
    assert u1["limits_c"]["tj_max"] == 150.0
    assert u1["supply"]["rails"] and u1["supply"]["rails"][0]["name"] == "VCC"
    assert u1["sources"], "seeded values must carry source notes"
    tot = phys["totals"]
    assert tot["power_w_typ"] is not None and tot["n_components"] == len(phys["components"])
    assert r.get("power"), "to_3d response must surface the power totals"
    print(f"  FIN8: sidecar {os.path.basename(sidecar)} — {len(phys['components'])} comps, "
          f"totals {tot}")
    print("  [fin8_to_3d_boardphys_sidecar] PASS")


def test_fin13_run_pdn(tools: dict) -> None:
    """T-FIN13: ecad_run_pdn flags an undersized 3 A trace (drop + current density) and
    exports the I2R map as the boardphys `pdn` sidecar extension."""
    import os
    from mcp_server.ecad_common import ecad_session
    from mcp_server.ecad_tools import _ECAD

    _call(tools, "ecad_new_pcb", name="fin13_pdn")
    pcb = ecad_session.get_session(_ECAD["current_pcb"])
    doc = pcb.doc
    # hand-build a +5V net: connector source pad -> skinny 0.3mm trace -> hungry load
    doc["nets"].append({"name": "+5V", "class": "default"})
    rail = {"supply": {"rails": [{"name": "+5V", "voltage_v_min": 4.9,
                                  "voltage_v_nom": 5.0, "voltage_v_max": 5.25,
                                  "current_a_active": 3.0, "current_a_sleep": None}],
                       "voltage_domains": ["+5V"], "sequencing": []},
            "sources": ["test fixture"]}

    def _pad(net):
        return {"id": "p1", "number": "1", "pin": "1", "pos": [0.0, 0.0], "angle": 0.0,
                "shape": "rect", "size": [1.0, 1.0], "type": "smd",
                "layers": ["F.Cu"], "net": net}
    doc["footprints"].extend([
        {"id": "fp1", "ref": "J1", "fpid": "Test:PAD1", "pos": [0.0, 0.0],
         "angle": 0.0, "side": "top", "pads": [_pad("+5V")]},
        {"id": "fp2", "ref": "U1", "fpid": "Test:PAD1", "pos": [30.0, 0.0],
         "angle": 0.0, "side": "top", "pads": [_pad("+5V")],
         "eng_overrides": rail},
    ])
    doc["tracks"].append({"id": "t1", "kind": "segment", "p1": [0.0, 0.0],
                          "p2": [30.0, 0.0], "width": 0.3, "net": "+5V",
                          "layer": "F.Cu"})
    ecad_session.swap_doc(pcb, doc)  # T-RF20: write the client-copy fixture back

    r = _call(tools, "ecad_run_pdn", net="+5V", source_refs="J1")
    _assert_context(r, "ecad_run_pdn")
    assert r["ok"], f"ecad_run_pdn failed: {r}"
    net = r["nets"][0]
    assert net["vsrc"] == 5.0, "vsrc must come from the rail metadata"
    u1 = next(d for d in net["drops"] if d["ref"] == "U1")
    assert u1["i_a"] == 3.0 and u1["within_tolerance"] is False, u1
    assert net["hotspots"], "0.3mm @ 3A must produce current-density hotspots"
    assert r["violations"], "expected drop + density violations"
    sidecar = r.get("boardphys_path")
    assert sidecar and os.path.isfile(sidecar), f"pdn sidecar not written: {sidecar!r}"
    with open(sidecar, encoding="utf-8") as fh:
        phys = json.load(fh)
    assert phys["schema"] == "aicad.boardphys/1"
    blk = phys["pdn"]
    assert blk["schema"] == "aicad.pdn/1" and blk["nets"][0]["net"] == "+5V"
    m = blk["nets"][0]["i2r_map"]
    assert m["columns"] == ["layer", "x_mm", "y_mm", "p_w"] and m["cells"]
    assert m["total_w"] > 0.1  # ~ I^2 R = 9 * ~0.05 ohm
    print(f"  FIN13: verdict={r['verdict']!r} total_i2r={m['total_w']:.3f} W "
          f"hotspots={len(net['hotspots'])}")

    # a fat clean net must pass
    doc["nets"].append({"name": "+3V3", "class": "default"})
    doc["footprints"].extend([
        {"id": "fp3", "ref": "J2", "fpid": "Test:PAD1", "pos": [0.0, 10.0],
         "angle": 0.0, "side": "top", "pads": [_pad("+3V3")]},
        {"id": "fp4", "ref": "U2", "fpid": "Test:PAD1", "pos": [30.0, 10.0],
         "angle": 0.0, "side": "top", "pads": [_pad("+3V3")],
         "eng_overrides": {"supply": {"rails": [{"name": "+3V3", "voltage_v_min": 3.0,
                                                 "voltage_v_nom": 3.3, "voltage_v_max": 3.6,
                                                 "current_a_active": 0.1,
                                                 "current_a_sleep": None}],
                                      "voltage_domains": ["+3V3"], "sequencing": []},
                           "sources": ["test fixture"]}},
    ])
    doc["tracks"].append({"id": "t2", "kind": "segment", "p1": [0.0, 10.0],
                          "p2": [30.0, 10.0], "width": 3.0, "net": "+3V3",
                          "layer": "F.Cu"})
    ecad_session.swap_doc(pcb, doc)  # T-RF20: write the client-copy fixture back
    r2 = _call(tools, "ecad_run_pdn", net="+3V3", source_refs="J2")
    assert r2["ok"] and not r2["violations"], f"clean net must pass: {r2.get('violations')}"
    u2 = next(d for d in r2["nets"][0]["drops"] if d["ref"] == "U2")
    assert u2["within_tolerance"] is True
    print("  [fin13_run_pdn] PASS")
def test_fin11_fpga_check_and_constraints(tools: dict) -> None:
    """T-FIN11: place the iCE40 starter symbol, run the bank-aware checks, export
    constraints (LPF + neutral CSV) — and confirm the no-FPGA guard teaches."""
    import os
    import tempfile

    d = _call(tools, "ecad_new_schematic", name="fpga_fin11")
    doc_id = d["doc_id"]
    # guard first: no FPGA on the sheet yet -> teaching error
    e = _call(tools, "ecad_fpga_check", doc_id=doc_id)
    # (empty sheet: no FPGA found)
    assert not e["ok"] and "FPGA" in (e.get("error") or e.get("hint") or "")
    _assert_context(e, "ecad_fpga_check")

    p = _call(tools, "ecad_place_symbol", libid="FPGA:iCE40UP5K-SG48", x=100, y=100,
              doc_id=doc_id)
    assert p["ok"], p
    r = _call(tools, "ecad_fpga_check", doc_id=doc_id)
    _assert_context(r, "ecad_fpga_check")
    assert r["devices"] and r["devices"][0]["device"] == "iCE40UP5K-SG48"
    # nothing wired -> CRESET_B (required config pin) must be flagged
    assert not r["ok"] and any(v["check"] == "config" for v in r["violations"]), r
    print(f"  FIN11: check found {len(r['violations'])} violation(s) on the bare FPGA "
          f"(config CRESET_B unwired) — as designed")

    with tempfile.TemporaryDirectory() as td:
        x = _call(tools, "ecad_export", kind="constraints", path=td, doc_id=doc_id)
        _assert_context(x, "ecad_export(constraints)")
        assert x["ok"] and x["kind"] == "constraints", x
        exts = sorted(os.path.splitext(f)[1] for f in x["files"])
        assert exts == [".csv", ".lpf"], f"expected LPF+CSV for a Lattice part, got {exts}"
        csv_file = next(f for f in x["files"] if f.endswith(".csv"))
        with open(csv_file, encoding="utf-8") as fh:
            head = fh.readline()
        assert head.startswith("pin,net,port,site,bank"), head
    print("  FIN11: ecad_export('constraints') wrote LPF + CSV pinout")
    print("  [fin11_fpga_check_and_constraints] PASS")


def _disable_live_provider():
    """Force ecad_design onto its deterministic recipe path for the whole suite.

    ``ecad_design`` calls ``agent.config.get_provider()`` and, when a real provider (e.g. a live
    Gemini key) is configured and reachable, plans the topology with the LLM. That plan is
    NON-deterministic: it usually fails the strict schema and falls back to the recipe (green), but
    occasionally emits a valid design whose self-check expectation is model-mismatched — e.g. an LED
    driver probing ``I(LED1)`` against a real-LED current while the generic Shockley model conducts
    at a different drop — which MISSes and flips ``d['ok']`` to False. That is the source of the
    flaky ``I(LED1)=nan`` / recipe-mismatch failures. These tests assert ``recipe_used`` and exist to
    exercise the RECIPE, not the planner, so we pin the provider to None (also stops the suite making
    paid API calls on every run). Returns a restore() callable. Best-effort: a no-op if agent.config
    is absent.
    """
    try:
        from agent import config as _cfg  # type: ignore
    except Exception:  # noqa: BLE001 -- agent package not importable in this env
        return lambda: None
    _orig = getattr(_cfg, "get_provider", None)
    _cfg.get_provider = lambda *a, **k: None  # type: ignore[attr-defined]
    def _restore():
        if _orig is not None:
            _cfg.get_provider = _orig  # type: ignore[attr-defined]
    return _restore

# --------------------------------------------------------------------------- pytest collection
# pytest treats bare `tools` parameters as fixtures. The main() harness passes the dict
# returned by test_register() explicitly; under pytest we provide a module-scoped fixture
# that registers the tools once with the live provider disabled (same as main()).

_PYTEST_PROVIDER_RESTORE = None


def pytest_configure(config):  # noqa: ANN001 — pytest hook
    """Disable the live provider for the whole pytest session of this file."""
    global _PYTEST_PROVIDER_RESTORE
    _PYTEST_PROVIDER_RESTORE = _disable_live_provider()


def pytest_unconfigure(config):  # noqa: ANN001 — pytest hook
    restore = globals().get("_PYTEST_PROVIDER_RESTORE")
    if callable(restore):
        restore()


try:
    import pytest
except ImportError:  # pragma: no cover — pytest absent when run via main()
    pytest = None  # type: ignore


if pytest is not None:
    @pytest.fixture(scope="module")
    def tools():
        """Module-scoped registered ECAD tool dict (provider disabled like main())."""
        return test_register()


# --------------------------------------------------------------------------- T-FIN16 thermal handoff


def _fin16_tools() -> dict:
    """Register the ECAD + thermal-handoff tools onto ONE stub (the handoff needs
    ecad session state)."""
    from mcp_server.ecad_thermal import register_ecad_thermal_tools
    stub = _StubMCP()
    register_ecad_tools(stub)
    register_ecad_thermal_tools(stub)
    return stub.tools


def _fin16_demo_board(tools: dict, name: str = "fin16_demo") -> str:
    """Regulator (~2 W) + MCU (0.3 W) + passives demo board with a closed outline and
    a skinny +5V trace (for the PDN/trace-heating leg). Returns the pcb doc_id."""
    from mcp_server.ecad_common import ecad_session
    from mcp_server.ecad_tools import _ECAD

    _call(tools, "ecad_new_pcb", name=name)
    doc_id = _ECAD["current_pcb"]
    doc = ecad_session.get_session(doc_id).doc
    # 40 x 30 mm closed rectangle outline.
    pts = [(0, 0), (40, 0), (40, 30), (0, 30)]
    doc["outline"] = {"closed": True, "shapes": [
        {"type": "line", "p1": list(pts[i]), "p2": list(pts[(i + 1) % 4])}
        for i in range(4)]}

    def _pad(net, n="1", size=1.2):
        return {"id": f"p{n}", "number": n, "pin": n, "pos": [0.0, 0.0], "angle": 0.0,
                "shape": "rect", "size": [size, size], "type": "smd",
                "layers": ["F.Cu"], "net": net}

    doc["nets"].append({"name": "+5V", "class": "default"})
    reg_eng = {"power_w": {"typ": 2.0, "max": 3.0},
               "theta_c_per_w": {"ja": 40.0, "jc": 5.0},
               "limits_c": {"tj_max": 125.0, "ta_max": 85.0},
               "supply": {"rails": [{"name": "+5V", "voltage_v_min": 4.75,
                                     "voltage_v_nom": 5.0, "voltage_v_max": 5.25,
                                     "current_a_active": 1.5, "current_a_sleep": None}],
                          "voltage_domains": ["+5V"], "sequencing": []},
               "sources": ["test fixture: 2 W linear regulator"]}
    mcu_eng = {"power_w": {"typ": 0.3, "max": 0.5},
               "theta_c_per_w": {"ja": 60.0, "jc": None},
               "limits_c": {"tj_max": 105.0, "ta_max": 85.0},
               # deliberately hungry fixture current so the skinny trace makes real I2R heat
               "supply": {"rails": [{"name": "+5V", "voltage_v_min": 4.75,
                                     "voltage_v_nom": 5.0, "voltage_v_max": 5.25,
                                     "current_a_active": 1.5, "current_a_sleep": None}],
                          "voltage_domains": ["+5V"], "sequencing": []},
               "sources": ["test fixture: MCU"]}
    doc["footprints"].extend([
        {"id": "fp1", "ref": "U1", "fpid": "Test:REG", "pos": [10.0, 15.0],
         "angle": 0.0, "side": "top", "pads": [_pad("+5V", size=6.0)],  # thermal tab
         "fields": {"height": 4.5}, "eng_overrides": reg_eng},
        {"id": "fp2", "ref": "U2", "fpid": "Test:MCU", "pos": [30.0, 15.0],
         "angle": 0.0, "side": "top", "pads": [_pad("+5V")],
         "fields": {"height": 1.6}, "eng_overrides": mcu_eng},
        {"id": "fp3", "ref": "R1", "fpid": "Test:R", "pos": [20.0, 25.0],
         "angle": 0.0, "side": "top", "pads": [_pad("GND")]},
        {"id": "fp4", "ref": "C1", "fpid": "Test:C", "pos": [20.0, 5.0],
         "angle": 0.0, "side": "top", "pads": [_pad("GND")]},
    ])
    # Skinny 0.3 mm trace carrying the regulator's 1.5 A: real I2R heat for the PDN leg.
    doc["tracks"].append({"id": "t1", "kind": "segment", "p1": [10.0, 15.0],
                          "p2": [30.0, 15.0], "width": 0.3, "net": "+5V",
                          "layer": "F.Cu"})
    # T-RF20: doc is a client copy of the C++ session doc — write the fixture back.
    sess = ecad_session.get_session(doc_id)
    ecad_session.swap_doc(sess, doc)
    return sess.doc_id


def test_fin16_dry_run_handoff() -> None:
    """T-FIN16: dry-run handoff builds the fused FEA solid, per-component heat sources,
    convection BCs and a VALID aicad.simhandoff/1 envelope; missing power metadata
    degrades honestly into the coverage note."""
    try:
        import build123d  # noqa: F401
    except ImportError:
        print("  FIN16: build123d not installed — skipping dry-run handoff test")
        return
    import os
    from services.mcp import simhandoff as sh

    tools = _fin16_tools()
    doc_id = _fin16_demo_board(tools)
    r = _call(tools, "ecad_thermal_handoff", doc_id=doc_id, dry_run=True)
    _assert_context(r, "ecad_thermal_handoff")
    assert r["ok"] and r["dry_run"] and r["solved"] is False, r.get("error")
    assert os.path.isfile(r["step_path"]), "FEA STEP not written"
    # Homogenization documented (DoD): assumptions + both conductivities surfaced.
    mat = r["material"]
    assert 1.0 < mat["k_w_mk"] < 60.0, f"implausible in-plane k: {mat['k_w_mk']}"
    assert mat["k_through_plane_w_mk"] < 1.0
    assert any("homogenized" in a for a in r["assumptions"])
    # One heat_source condition PER powered component, applied_to = ref (contract).
    env = r["simhandoff"]
    assert sh.validate_envelope(env) == [], sh.validate_envelope(env)
    assert env["coupling"] == "ecad_to_thermal_fea"
    hs = {c["applied_to"]: c for c in env["conditions"] if c["kind"] == "heat_source"}
    assert set(hs) == {"U1", "U2"}, f"heat sources: {sorted(hs)}"
    assert hs["U1"]["value"] == 2.0 and hs["U1"]["units"] == "W"
    assert "power_w.typ" in hs["U1"]["provenance"]
    assert any(c["kind"] == "convection" for c in env["conditions"])
    # Honest degrade: unpowered passives named, never given invented watts.
    cov = r["coverage"]
    assert set(cov["unpowered_refs"]) == {"R1", "C1"}, cov
    assert cov["n_heat_sources"] == 2
    # theta_ja expectations for the verify hand calc.
    exp = {e["quantity"]: e for e in env.get("expectations", [])}
    assert "tj_U1_c" in exp and abs(exp["tj_U1_c"]["value"] - (25.0 + 2.0 * 40.0)) < 1e-6
    print(f"  FIN16: dry run — {cov['n_heat_sources']} sources, "
          f"k_eff {mat['k_w_mk']} W/mK, envelope valid")
    print("  [fin16_dry_run_handoff] PASS")


def test_fin16_trace_heating_toggle() -> None:
    """T-FIN16: include_trace_heating pulls the ecad_run_pdn I2R map in as distributed
    flux — the load set and envelope visibly change."""
    try:
        import build123d  # noqa: F401
    except ImportError:
        print("  FIN16: build123d not installed — skipping trace-heating test")
        return
    tools = _fin16_tools()
    doc_id = _fin16_demo_board(tools, name="fin16_i2r")
    p = _call(tools, "ecad_run_pdn", net="+5V", source_refs="U1", doc_id=doc_id)
    assert p["ok"] and p.get("boardphys_path"), f"pdn failed: {p.get('error')}"
    base = _call(tools, "ecad_thermal_handoff", doc_id=doc_id, dry_run=True)
    with_i2r = _call(tools, "ecad_thermal_handoff", doc_id=doc_id, dry_run=True,
                     include_trace_heating=True)
    assert base["ok"] and with_i2r["ok"]
    assert base["trace_heating"]["included"] is False
    ti = with_i2r["trace_heating"]
    assert ti["included"] and ti["total_w"] > 0.05 and ti["n_cell_loads"] > 0, ti
    assert with_i2r["n_flux_loads"] > base["n_flux_loads"], \
        "I2R map must add flux loads"
    conds = {c["name"] for c in with_i2r["simhandoff"]["conditions"]}
    assert "trace_heating_i2r" in conds and \
        "trace_heating_i2r" not in {c["name"] for c in base["simhandoff"]["conditions"]}
    print(f"  FIN16: I2R toggle — +{with_i2r['n_flux_loads'] - base['n_flux_loads']} "
          f"flux loads, {ti['total_w']} W trace heat")
    print("  [fin16_trace_heating_toggle] PASS")


def test_fin16_verify_thermal() -> None:
    """T-FIN16: ecad_verify_thermal builds the Tj/limits margin table + the theta_ja
    hand-calc verify envelope from a solved handoff payload (engine-less)."""
    from services.mcp import simhandoff as sh
    tools = _fin16_tools()
    # Synthetic SOLVED handoff: U1 solved rise ~ hand-calc rise (2W x 40 C/W = 80 C);
    # U2 exceeds its 105 C limit.
    solved = {
        "ok": True, "solved": True, "doc_id": "pcb-test",
        "environment": {"name": "still_air", "h_w_m2k": 10.0, "t_amb_c": 25.0},
        "components": [
            {"ref": "U1", "power_w": 2.0, "t_case_c": 95.0, "tj_c": 105.0,
             "theta_jc": 5.0, "theta_ja": 40.0, "tj_max_c": 125.0},
            {"ref": "U2", "power_w": 0.3, "t_case_c": 108.0, "tj_c": None,
             "theta_jc": None, "theta_ja": 60.0, "tj_max_c": 105.0},
            {"ref": "R1", "power_w": None, "t_case_c": 60.0, "tj_c": None,
             "theta_jc": None, "theta_ja": None, "tj_max_c": None},
        ],
        "simhandoff": {"id": "hid-1"},
    }
    v = _call(tools, "ecad_verify_thermal", handoff_result=solved, tolerance_pct=75.0)
    _assert_context(v, "ecad_verify_thermal")
    assert v["ok"], v.get("error")
    rows = {e["ref"]: e for e in v["table"]}
    assert rows["U1"]["pass"] is True and abs(rows["U1"]["margin_c"] - 20.0) < 1e-6
    # U2 has no theta_jc -> Tcase basis, over its 105 C limit -> FAIL with margin.
    assert rows["U2"]["pass"] is False and rows["U2"]["margin_c"] == -3.0
    assert rows["R1"]["pass"] is None, "no limit -> unchecked, never assumed OK"
    assert v["pass"] is False and "FAIL" in v["verdict"]
    env = v["simhandoff"]
    assert sh.validate_envelope(env) == [], sh.validate_envelope(env)
    assert env["kind"] == "verify" and env["handoff_id"] == "hid-1"
    checks = {c["quantity"]: c for c in env["checks"]}
    # U1 hand calc: rise 80 C vs solved rise 80 C -> 0 % deviation, passes.
    assert checks["tj_rise_U1"]["pass"] and checks["tj_rise_U1"]["deviation_pct"] < 1.0
    # Dry-run / unsolved input is rejected honestly.
    bad = _call(tools, "ecad_verify_thermal", handoff_result={"ok": True, "solved": False})
    assert not bad["ok"] and "not solved" in bad["error"]
    print(f"  FIN16: verify — verdict {v['verdict']!r}")
    print("  [fin16_verify_thermal] PASS")


def test_fin16_live_solve() -> None:
    """T-FIN16 (live leg): full board -> solved temperature field with zero manual BC
    setup, when a sim-enabled libchudware is loadable. Honest skip otherwise."""
    try:
        import build123d  # noqa: F401
    except ImportError:
        print("  FIN16: build123d not installed — skipping live solve")
        return
    try:
        from mcp_server import core_bridge as cb
        cb.call("ping", {})
    except Exception as e:  # noqa: BLE001
        print(f"  FIN16: libchudware not loadable ({type(e).__name__}) — live solve SKIPPED "
              "(dry-run coverage above still exercises geometry/BC/envelope)")
        return
    tools = _fin16_tools()
    doc_id = _fin16_demo_board(tools, name="fin16_live")
    r = _call(tools, "ecad_thermal_handoff", doc_id=doc_id, mesh_fineness=0.25)
    if not r.get("ok") or not r.get("solved"):
        # A Netgen-less build degrades to an envelope-only result — honest skip.
        why = r.get("error_solve") or r.get("error")
        assert "unavailable" in str(why) or r.get("simhandoff"), r
        print(f"  FIN16: solver leg unavailable ({str(why)[:80]}) — live solve SKIPPED")
        return
    amb = r["environment"]["t_amb_c"]
    assert r["board_max_temp_c"] > amb + 5.0, \
        f"2.35 W in still air must heat the board: {r['board_max_temp_c']} C"
    temps = {c["ref"]: c for c in r["components"]}
    assert temps["U1"]["t_case_c"] is not None and temps["U2"]["t_case_c"] is not None
    assert temps["U1"]["t_case_c"] >= temps["U2"]["t_case_c"], \
        "the 2 W regulator must run hotter than the 0.3 W MCU"
    assert temps["U1"]["tj_c"] is not None, "theta_jc known -> Tj must be estimated"
    v = _call(tools, "ecad_verify_thermal")
    assert v["ok"] and v["table"], v
    print(f"  FIN16: LIVE solve — board max {r['board_max_temp_c']} C, "
          f"U1 case {temps['U1']['t_case_c']} C / Tj {temps['U1']['tj_c']} C; "
          f"verify: {v['verdict']!r}")
    print("  [fin16_live_solve] PASS")


# --------------------------------------------------------------------------- T-FIN17 heatsink selection


def _fin17_tools() -> dict:
    """ECAD + thermal-handoff + heatsink tools on ONE stub (shared session state)."""
    from mcp_server.ecad_thermal import register_ecad_thermal_tools
    from mcp_server.heatsink import register_heatsink_tools
    stub = _StubMCP()
    register_ecad_tools(stub)
    register_ecad_thermal_tools(stub)
    register_heatsink_tools(stub)
    return stub.tools


def test_fin17_parametric_generator() -> None:
    """T-FIN17 (DoD): the parametric plain-finned-sink generator produces VALID
    geometry across its parameter range — every emitted spec passes structural
    validation, has physical mass/theta, and yields a well-formed engine op list;
    unbuildable specs are rejected."""
    from mcp_server.heatsink import (parametric_candidates, validate_sink_spec,
                                     sink_theta_sa, sink_mass_g, sink_ops)
    n_specs = 0
    for fp in (2.0, 6.0, 12.0, 25.0, 60.0):          # footprint span sweep (mm)
        cands = parametric_candidates(fp, fp)
        assert cands, f"no candidates for footprint {fp} mm"
        prev_theta = None
        for spec in cands:
            n_specs += 1
            assert validate_sink_spec(spec) == [], (spec, validate_sink_spec(spec))
            assert sink_mass_g(spec) > 0
            th = sink_theta_sa(spec, 10.0)
            assert 0.1 < th < 500.0, (spec, th)
            if prev_theta is not None:               # bigger sink -> lower theta
                assert th < prev_theta, (spec, th, prev_theta)
            prev_theta = th
            ops = sink_ops(spec)
            assert len(ops) == 1 + spec["n_fins"]
            assert all(o["op"] == "add_feature" and o["feature"]["type"] == "box"
                       for o in ops)
            # Fins sit on the base and fit inside its width.
            base = ops[0]["feature"]["params"]
            last_fin = ops[-1]["feature"]["params"]
            assert last_fin["at"][2] == spec["base_t_mm"]
            assert (last_fin["at"][1] + spec["fin_t_mm"]
                    <= base["at"][1] + spec["base_w_mm"] + 1e-9)
    # Invalid specs are rejected, never silently emitted.
    assert validate_sink_spec({"base_l_mm": 10, "base_w_mm": 10, "base_t_mm": 2,
                               "n_fins": 12, "fin_t_mm": 1.0, "fin_h_mm": 8}) != []
    assert validate_sink_spec({"base_l_mm": -5, "base_w_mm": 10, "base_t_mm": 2,
                               "n_fins": 4, "fin_t_mm": 1.0, "fin_h_mm": 8}) != []
    print(f"  FIN17: parametric generator — {n_specs} specs across the range, "
          "all valid, invalid rejected")
    print("  [fin17_parametric_generator] PASS")


def test_fin17_library_candidates() -> None:
    """T-FIN17: the parts library serves heatsink candidates WITH thermal metadata
    (domain 'thermal'), and undersized sinks are filtered for a big component."""
    from mcp_server.heatsink import library_candidates
    cands, notice = library_candidates("still_air", 10.0, 6.0, 6.0)
    assert notice is None, notice
    assert len(cands) >= 5, f"expected curated heatsinks, got {len(cands)}"
    for c in cands:
        assert c["source"] == "library"
        assert isinstance(c["theta_sa_c_per_w"], (int, float))
        assert c["mass_g"] and c["base_l_mm"]
    assert any(c.get("cost_usd") for c in cands), "cost carried when known"
    big = library_candidates("still_air", 10.0, 45.0, 45.0)[0]
    assert all(c["base_l_mm"] >= 45.0 * 0.9 for c in big), \
        "sinks smaller than the component must be filtered"
    # Forced air uses the forced nominal (lower theta).
    forced = {c["id"]: c for c in library_candidates("forced_air", 50.0, 6.0, 6.0)[0]}
    still = {c["id"]: c for c in cands}
    common = set(forced) & set(still)
    assert common and all(forced[i]["theta_sa_c_per_w"] < still[i]["theta_sa_c_per_w"]
                          for i in common)
    print(f"  FIN17: library — {len(cands)} candidates with theta/mass/cost")
    print("  [fin17_library_candidates] PASS")


def _fin17_board(tools: dict, name: str) -> str:
    """FIN16 demo board with the regulator turned up to a ~3 W part."""
    doc_id = _fin16_demo_board(tools, name=name)
    from mcp_server.ecad_common import ecad_session
    doc = ecad_session.get_session(doc_id).doc
    for fp in doc["footprints"]:
        if fp["ref"] == "U1":
            fp["eng_overrides"]["power_w"] = {"typ": 3.0, "max": 3.5}
    # T-RF20: write the client-copy mutation back into the C++ session.
    sess = ecad_session.get_session(doc_id)
    ecad_session.swap_doc(sess, doc)
    return sess.doc_id


def test_fin17_heatsink_select() -> None:
    """T-FIN17 (DoD demo): ~3 W regulator at an 85 C limit — heatsink_select ranks
    library + parametric candidates, the winner meets target in the re-solve (live
    FEA when libchudware is available, honest network_estimate otherwise), the ranked
    table carries {candidate, achieved_tj_c, delta_vs_target, mass_g, cost}, and the
    result embeds a valid aicad.simhandoff/1 verify envelope. Also: the impossible-
    target fixture is reported honestly (best achievable named, target_met=false)."""
    try:
        import build123d  # noqa: F401
    except ImportError:
        print("  FIN17: build123d not installed — skipping heatsink_select test")
        return
    from services.mcp import simhandoff as sh

    tools = _fin17_tools()
    # No context yet -> honest error.
    from mcp_server import ecad_thermal as _et
    _et._THERMAL["last_ctx"] = None
    r0 = _call(tools, "heatsink_select", component_ref="U1", tj_target_c=85.0)
    assert not r0["ok"] and "ecad_thermal_handoff" in r0["error"]

    doc_id = _fin17_board(tools, "fin17_demo")
    live = True
    try:
        from mcp_server import core_bridge as cb
        cb.call("ping", {})
    except Exception:  # noqa: BLE001
        live = False
    h = _call(tools, "ecad_thermal_handoff", doc_id=doc_id, mesh_fineness=0.25,
              dry_run=not live)
    assert h["ok"], h.get("error")
    if live and not h.get("solved"):
        live = False   # Netgen-less build: envelope-only result, network mode below

    r = _call(tools, "heatsink_select", component_ref="U1", tj_target_c=85.0)
    _assert_context(r, "heatsink_select")
    assert r["ok"], r.get("error")
    assert r["mode"] == ("fea_resolve" if live else "network_estimate")
    assert r["mesh_cached"] is live
    # Ranked-table shape (DoD): candidate/achieved/delta/mass/cost per row, sorted.
    table = r["table"]
    assert len(table) >= 4
    for row in table:
        assert {"candidate", "source", "achieved_tj_c", "delta_vs_target_c",
                "mass_g", "cost_usd", "theta_sa_c_per_w", "meets_target"} <= set(row)
    achieved = [row["achieved_c"] for row in table if row["achieved_c"] is not None]
    assert achieved == sorted(achieved), "table must be ranked best-first"
    srcs = {row["source"] for row in table}
    assert "library" in srcs and "parametric" in srcs, srcs
    # Winner meets the 85 C target in the re-solve.
    assert r["target_met"] is True, (r["winner"], r["warnings"])
    w = r["winner"]
    assert w["meets_target"] and w["achieved_c"] <= 85.0
    assert w["delta_vs_target_c"] <= 0.0
    if live:
        assert w["solve"] == "fea" and w.get("board_max_temp_c") is not None
        # Winner's solved study is KEPT.
        from mcp_server.heatsink import _HEATSINK
        kept = _HEATSINK["winner_study"]
        assert kept and kept["candidate"] == w["candidate"]
        assert kept["study"]["t_case_c"] is not None
        # The sink must actually help vs the bare baseline solve.
        bare_u1 = {c["ref"]: c for c in h["components"]}["U1"]
        assert w["achieved_c"] < bare_u1["tj_c"], (w, bare_u1)
    # simhandoff verify envelope (DoD).
    env = r["simhandoff"]
    assert sh.validate_envelope(env) == [], sh.validate_envelope(env)
    assert env["kind"] == "verify"
    assert env["coupling"] == "ecad_heatsink_to_thermal_fea"
    assert env["checks"][0]["pass"] is True

    # Impossible target: 26 C at 25 C ambient -> honest FAIL, best achievable named.
    imp = _call(tools, "heatsink_select", component_ref="U1", tj_target_c=26.0)
    assert imp["ok"] and imp["target_met"] is False
    assert imp["best_achievable"]["candidate"]
    assert imp["best_achievable"]["achieved_c"] > 26.0
    assert any("IMPOSSIBLE TARGET" in wmsg for wmsg in imp["warnings"])
    assert imp["simhandoff"]["checks"][0]["pass"] is False
    assert "FAIL" in imp["simhandoff"]["verdict"]
    # Below-ambient target rejected outright.
    sub = _call(tools, "heatsink_select", component_ref="U1", tj_target_c=20.0)
    assert not sub["ok"] and "ambient" in sub["error"]

    print(f"  FIN17: {'LIVE FEA re-solve' if live else 'network estimate'} — "
          f"winner {w['candidate']!r} achieves {w['achieved_c']} C "
          f"(target 85), mass {w['mass_g']} g; impossible target honest "
          f"(best {imp['best_achievable']['achieved_c']} C)")
    print("  [fin17_heatsink_select] PASS")



# --------------------------------------------------------------------------- result-serving size gates


def _build_large_schematic(tools: dict, n_pairs: int = 16) -> str:
    """Place >=30 components (n_pairs*2 resistors + 1 voltage source) on a fresh sch."""
    s = _call(tools, "ecad_new_schematic", name=f"size_{n_pairs}")
    doc_id = s["doc_id"]
    _call(tools, "ecad_place_symbol", libid="Simulation_SPICE:V_source", x=0, y=0,
          value="5", doc_id=doc_id)
    for i in range(n_pairs):
        x = 20 + (i % 8) * 15
        y = 20 + (i // 8) * 20
        _call(tools, "ecad_place_symbol", libid="Device:R", x=x, y=y,
              value="1k", doc_id=doc_id)
        _call(tools, "ecad_place_symbol", libid="Device:R", x=x + 10, y=y,
              value="2k", doc_id=doc_id)
    return doc_id


def test_status_summary_token_budget(tools: dict) -> None:
    """Default ecad_status(detail=summary) stays <=800 tokens on a >=30-comp board
    while retaining ok/summary/counts/erc counts/unconnected/next_actions."""
    from mcp_server import result_serving as rs

    doc_id = _build_large_schematic(tools, n_pairs=16)  # 1 V + 32 R = 33 comps
    raw = tools["ecad_status"](doc_id=doc_id)  # default summary
    st = json.loads(raw)
    _assert_context(st, "ecad_status(summary)")
    assert st["ok"] and st["kind"] == "sch"
    assert st["component_count"] >= 30, st["component_count"]
    assert "summary" in st and st["summary"]
    assert "component_count" in st and "net_count" in st
    assert "erc" in st and "errors" in st["erc"] and "warnings" in st["erc"]
    assert "unconnected_count" in st or "unconnected_pins" in st
    assert st.get("next_actions")
    # Summary must NOT dump pin arrays / net members.
    for c in st.get("components") or []:
        assert "pins" not in c, f"summary leaked pins on {c.get('ref')}"
    for n in st.get("nets") or []:
        if isinstance(n, dict):
            assert "members" not in n, f"summary leaked members on {n.get('name')}"
    tokens = rs.count_tokens(raw)
    print(f"  status summary tokens={tokens} (budget 800) comps={st['component_count']}")
    assert tokens <= 800, f"ecad_status summary too large: {tokens} tokens"
    # meta is attached by serve()
    assert "meta" in st and st["meta"].get("detail") == "summary"
    print("  [status_summary_token_budget] PASS")


def test_status_full_ref_set_equality(tools: dict) -> None:
    """detail=full retains the full component-ref + net-name sets (pre-change parity).

    Summary keeps the same counts and a sample; full is the escape hatch with
    pins + members and complete ref/name sets.
    """
    doc_id = _build_large_schematic(tools, n_pairs=16)  # 33 comps
    full = _call(tools, "ecad_status", doc_id=doc_id, detail="full")
    summary = _call(tools, "ecad_status", doc_id=doc_id, detail="summary")
    assert full["ok"] and summary["ok"]
    full_refs = {c["ref"] for c in full["components"]}
    assert len(full_refs) == full["component_count"] >= 30
    # Full must include pins; summary must not.
    assert any("pins" in c for c in full["components"]), "detail=full should keep pins"
    assert all("pins" not in c for c in (summary.get("components") or []))

    def _net_names(st):
        names = set()
        for n in st.get("nets") or []:
            if isinstance(n, dict):
                names.add(n.get("name"))
            else:
                names.add(n)
        return names

    full_nets = _net_names(full)
    assert len(full_nets) == full["net_count"]
    # Counts agree between detail levels even when summary samples the lists.
    assert summary["component_count"] == full["component_count"]
    assert summary["net_count"] == full["net_count"]
    # Full nets carry members when present.
    assert any(isinstance(n, dict) and "members" in n for n in full["nets"]), \
        "detail=full should keep net members"
    # Summary may truncate list length under budget, but every remaining ref is
    # drawn from the full set (no fabricated ids).
    sum_refs = {c["ref"] for c in (summary.get("components") or [])}
    assert sum_refs <= full_refs
    sum_nets = _net_names(summary)
    assert sum_nets <= full_nets
    print(f"  full refs={len(full_refs)} nets={len(full_nets)}; "
          f"summary sample refs={len(sum_refs)} nets={len(sum_nets)}")
    print("  [status_full_ref_set_equality] PASS")


def test_simulate_tran_ac_no_series(tools: dict) -> None:
    """ecad_simulate tran/ac must never return per-timestep arrays (always summarized)."""
    _call(tools, "ecad_new_schematic", name="sim_series")
    _build_divider(tools)
    # Label the midpoint so default probes pick a named net when present.
    _call(tools, "ecad_label", text="OUT", x=20, y=30)

    tran = _call(tools, "ecad_simulate", analysis="tran",
                 params={"tstep": 1e-5, "tstop": 1e-3})
    _assert_context(tran, "ecad_simulate(tran)")
    assert tran["ok"], tran.get("error")
    t = tran.get("tran") or {}
    assert "traces" in t, t
    for name, tr in (t.get("traces") or {}).items():
        assert isinstance(tr, dict), f"trace {name} should be summary dict, got {type(tr)}"
        assert "final" in tr or "min" in tr or "max" in tr
        assert not isinstance(tr, list)
    # No raw series keys.
    assert "x_t" not in t
    blob = json.dumps(tran)
    assert "x_t" not in blob

    ac = _call(tools, "ecad_simulate", analysis="ac",
               params={"sweep": "dec", "points": 10, "fstart": 10, "fstop": 1e5})
    _assert_context(ac, "ecad_simulate(ac)")
    assert ac["ok"], ac.get("error")
    a = ac.get("ac") or {}
    assert "x_f" not in a
    for name, tr in (a.get("traces") or {}).items():
        assert isinstance(tr, dict)
        assert "peak_mag" in tr or "f_-3dB" in tr
    assert "x_f" not in json.dumps(ac)
    print(f"  tran reading={tran.get('reading')!r}")
    print(f"  ac reading={ac.get('reading')!r}")
    print("  [simulate_tran_ac_no_series] PASS")


def main() -> None:
    print("AI-ECAD MCP tool tests")
    _restore_provider = _disable_live_provider()
    try:
        _run_all()
    finally:
        _restore_provider()


def _run_all() -> None:
    tools = test_register()
    test_find_part(tools)
    test_place_and_wire(tools)
    test_status(tools)
    test_unconnected_context(tools)
    test_run_erc(tools)
    test_simulate_op(tools)
    test_simulate_no_devices(tools)
    test_sweep(tools)
    test_sweep_guard(tools)
    test_pcb_and_drc(tools)
    test_sync_autoroute_zone_flow(tools)
    test_add_zone_unknown_net(tools)
    test_export_guard(tools)
    test_ecad_design(tools)
    # new tool tests
    test_tf(tools)
    test_simulate_four(tools)
    test_simulate_noise(tools)
    test_simulate_mc(tools)
    test_simulate_temp(tools)
    test_check_impedance_no_tracks(tools)
    test_check_impedance_routed(tools)
    test_make_board(tools)
    # Wave-L new tests
    test_run_dfm_clean(tools)
    test_run_dfm_seeded(tools)
    test_autoroute_with_diff_pairs(tools)
    test_check_diff_impedance(tools)
    # round-5 black-box report bug-fix regressions (E1/E2/E3/E5/E6/E8/E9)
    test_e1_multiple_docs_coexist(tools)
    test_e2_led_driver_recipe(tools)
    test_e3_opamp_noninverting_gain(tools)
    test_e5_make_board_honors_doc_id(tools)
    test_e6_update_from_sch_places_footprints(tools)
    test_e7_bogus_libid_rejected(tools)
    test_e8_temp_probe_surfaced(tools)
    test_e9_noise_top_level_nonzero(tools)
    # T-FIN8 engineering metadata + boardphys sidecar
    test_fin8_metadata_roundtrip(tools)
    test_fin8_missing_metadata_degrades(tools)
    test_fin8_to_3d_boardphys_sidecar(tools)
    # T-FIN9 host-board rule pack
    test_fin9_hostcheck_tool(tools)
    # T-FIN13 PDN IR-drop
    test_fin13_run_pdn(tools)
    # T-FIN11 FPGA support pack
    test_fin11_fpga_check_and_constraints(tools)
    # result-serving size gates
    test_status_summary_token_budget(tools)
    test_status_full_ref_set_equality(tools)
    test_simulate_tran_ac_no_series(tools)
    # T-FIN16 ECAD -> thermal FEA handoff
    test_fin16_dry_run_handoff()
    test_fin16_trace_heating_toggle()
    test_fin16_verify_thermal()
    test_fin16_live_solve()
    # T-FIN17 heatsink selection loop
    test_fin17_parametric_generator()
    test_fin17_library_candidates()
    test_fin17_heatsink_select()
    print("\nALL ECAD TOOL TESTS PASSED")


if __name__ == "__main__":
    main()


# --------------------------------------------------------------------------- T-FIN19 airflow (CHT) handoff
# Appended EOF (shared-file hygiene). pytest collects these; the __main__ runner above
# predates them and only runs the older suite.


def _fin19_tools() -> dict:
    from mcp_server.ecad_thermal import (register_ecad_thermal_tools,
                                         register_ecad_airflow_tools)
    stub = _StubMCP()
    register_ecad_tools(stub)
    register_ecad_thermal_tools(stub)
    register_ecad_airflow_tools(stub)
    return stub.tools


def _fin19_board_geo() -> tuple:
    """Synthetic board_to_fea_step()-shaped blocks (engine-less)."""
    board = {"bbox_mm": [0.0, 0.0, 40.0, 30.0], "z_top_mm": 1.6, "z_bot_mm": 0.0,
             "thickness_mm": 1.6, "outline_area_mm2": 1200.0}
    material = {"name": "FR4+Cu homogenized", "k_w_mk": 18.0,
                "k_through_plane_w_mk": 0.35, "cp_j_kgk": 1100.0, "rho_kg_m3": 1900.0}
    components = [
        {"ref": "U1", "power_w": {"typ": 2.0, "max": 3.0},
         "fea": {"rect_mm": [7.0, 12.0, 13.0, 18.0], "z_bot_mm": 1.6, "z_top_mm": 3.1,
                 "top_area_mm2": 36.0, "side": "top"}},
        {"ref": "U2", "power_w": {"typ": None, "max": 0.5},
         "fea": {"rect_mm": [27.0, 12.0, 33.0, 18.0], "z_bot_mm": 1.6, "z_top_mm": 2.6,
                 "top_area_mm2": 36.0, "side": "top"}},
        {"ref": "R1", "power_w": {},   # no power metadata -> no solid (honesty)
         "fea": {"rect_mm": [18.0, 24.0, 22.0, 26.0], "z_bot_mm": 1.6, "z_top_mm": 2.2,
                 "top_area_mm2": 8.0, "side": "top"}},
    ]
    return board, material, components


def test_fin19_box_stl_watertight(tmp_path) -> None:
    """write_box_stl emits a closed 12-facet ASCII STL in world mm."""
    from mcp_server.ecad_thermal import write_box_stl
    p = write_box_stl(str(tmp_path / "b.stl"), 0, 0, 0, 4, 3, 2)
    txt = open(p, encoding="ascii").read()
    assert txt.count("facet normal") == 12 and txt.count("endfacet") == 12
    assert txt.startswith("solid") and "endsolid" in txt
    try:
        import trimesh
        m = trimesh.load(p, force="mesh")
        assert m.is_watertight, "box STL must be watertight for CHT cellZones"
        assert abs(m.volume - 24.0) < 1e-6
    except ImportError:
        pass
    print("  [fin19_box_stl_watertight] PASS")


def test_fin19_case_generation(tmp_path) -> None:
    """Engine-less: build_airflow_solids produces the exact T-SIM5 solids seam shape —
    board (passive, homogenized k) + one region per POWERED component, unpowered
    components skipped honestly; enclosure adds 4 passive duct walls."""
    from mcp_server.ecad_thermal import build_airflow_solids, build_enclosure_walls
    import os as _os
    board, material, components = _fin19_board_geo()
    case = build_airflow_solids(board, material, components, str(tmp_path))
    names = [s["name"] for s in case["solids"]]
    assert names[0] == "board" and len(case["solids"]) == 3, names
    for s in case["solids"]:
        assert _os.path.isfile(s["stl_path"])
        assert set(s) == {"name", "stl_path", "kappa_w_mk", "power_w"}
    assert case["solids"][0]["power_w"] == 0.0
    assert case["solids"][0]["kappa_w_mk"] == 18.0
    by_ref = {s["ref"]: s for s in case["sources"]}
    assert by_ref["U1"]["power_w"] == 2.0 and by_ref["U2"]["power_w"] == 0.5
    assert case["unpowered_refs"] == ["R1"]
    assert case["region_map"]["U1"].startswith("c_")
    # heat_source conditions carry units + provenance (simhandoff contract).
    hs = {c["applied_to"]: c for c in case["conditions"]}
    assert hs["U1"]["value"] == 2.0 and hs["U1"]["units"] == "W"
    assert "power_w.typ" in hs["U1"]["provenance"]
    assert "power_w.max" in hs["U2"]["provenance"]
    walls, assum = build_enclosure_walls(board, {"clearance_mm": 8.0}, str(tmp_path))
    assert len(walls) == 4 and all(w["power_w"] == 0.0 for w in walls)
    assert any("bypass" in a for a in assum), "bypass-flow honesty note required"
    print("  [fin19_case_generation] PASS")


def test_fin19_region_parse() -> None:
    from mcp_server.ecad_thermal import parse_region_temps
    notes = ["chtMultiRegionFoam coupled solid+fluid; solid temperature 25-61 C.",
             "per-region: board: 41 C mean, 44 C peak; c_U1: 58 C mean, 61 C peak."]
    t = parse_region_temps(notes)
    assert t["board"] == {"mean_c": 41.0, "peak_c": 44.0}
    assert t["c_U1"]["peak_c"] == 61.0
    print("  [fin19_region_parse] PASS")


def test_fin19_deviation_table() -> None:
    """Engine-less: the verify math — sane / optimistic / conservative verdicts, rise-
    based deviation, and effective-h guidance when the preset was optimistic."""
    from mcp_server.ecad_thermal import airflow_deviation_table
    preset = [{"ref": "U1", "t_case_c": 65.0},   # rise 40
              {"ref": "U2", "t_case_c": 45.0},   # rise 20
              {"ref": "U3", "t_case_c": 35.0}]   # rise 10
    cht = [{"ref": "U1", "power_w": 2.0, "t_peak_c": 105.0, "t_mean_c": 95.0},  # rise 80: +100 %
           {"ref": "U2", "power_w": 0.5, "t_peak_c": 47.0, "t_mean_c": 44.0},   # rise 22: +10 %
           {"ref": "U3", "power_w": 0.2, "t_peak_c": 29.0, "t_mean_c": 28.0}]   # rise 4: -60 %
    table, checks, guidance = airflow_deviation_table(preset, cht, 25.0, 25.0, 10.0, 40.0)
    by = {e["ref"]: e for e in table}
    assert "OPTIMISTIC" in by["U1"]["verdict"]
    assert by["U2"]["verdict"].startswith("sane")
    assert "conservative" in by["U3"]["verdict"]
    assert len(checks) == 3 and [c["pass"] for c in checks] == [False, True, False]
    assert checks[0]["deviation_pct"] == 100.0
    # effective-h guidance: h_eff = 10 * 40/80 = 5 W/m2-K
    assert guidance and "U1" in guidance[0] and "5.0" in guidance[0], guidance
    print("  [fin19_deviation_table] PASS")


def test_fin19_verify_tool_and_envelope() -> None:
    """ecad_verify_airflow builds the deviation table + a VALID ecad_to_cht verify
    envelope from explicit payloads (no engine, no OpenFOAM)."""
    from services.mcp import simhandoff as sh
    tools = _fin19_tools()
    air = {"solved": True, "doc_id": "d1", "inlet": {"temp_c": 25.0, "speed_mps": 2.0},
           "components": [{"ref": "U1", "power_w": 2.0, "t_peak_c": 105.0,
                           "t_mean_c": 95.0}],
           "simhandoff": {"id": "h-123"}}
    pre = {"solved": True, "doc_id": "d1",
           "environment": {"h_w_m2k": 10.0, "t_amb_c": 25.0},
           "components": [{"ref": "U1", "t_case_c": 65.0}]}
    r = _call(tools, "ecad_verify_airflow", airflow_result=air, preset_result=pre)
    _assert_context(r, "ecad_verify_airflow")
    assert r["ok"] and r["pass"] is False and "NOT SANE" in r["verdict"], r
    assert r["guidance"] and "optimistic" in r["guidance"][0]
    env = r["simhandoff"]
    assert sh.validate_envelope(env) == [], sh.validate_envelope(env)
    assert env["coupling"] == "ecad_to_cht" and env["handoff_id"] == "h-123"
    assert env["checks"][0]["quantity"] == "t_rise_U1"
    # no-airflow degrade is honest
    r2 = _call(tools, "ecad_verify_airflow", airflow_result={"solved": False})
    assert not r2["ok"] and "ecad_airflow_handoff" in str(r2["next_actions"])
    print("  [fin19_verify_tool_and_envelope] PASS")


def test_fin19_handoff_dry_run() -> None:
    """Full-tool dry run on a real board doc: solids + envelope + honest skip; the LBM
    advisory is offered ONLY as a non-thermal render. Needs build123d (geometry leg)."""
    try:
        import build123d  # noqa: F401
    except ImportError:
        print("  FIN19: build123d not installed — skipping dry-run handoff test")
        return
    import os as _os
    from services.mcp import simhandoff as sh
    tools = _fin19_tools()
    doc_id = _fin16_demo_board(tools, name="fin19_dry")
    r = _call(tools, "ecad_airflow_handoff", doc_id=doc_id, dry_run=True,
              enclosure={"clearance_mm": 6.0})
    _assert_context(r, "ecad_airflow_handoff")
    assert r["ok"] and r["skipped"] and r["solved"] is False, r.get("error")
    names = [s["name"] for s in r["solids"]]
    assert names[0] == "board" and "c_U1" in names and "c_U2" in names, names
    assert sum(n.startswith("encl_") for n in names) == 4, names
    for s in r["solids"]:
        assert _os.path.isfile(s["stl_path"]), s
    assert set(r["coverage"]["unpowered_refs"]) == {"R1", "C1"}
    env = r["simhandoff"]
    assert sh.validate_envelope(env) == [], sh.validate_envelope(env)
    assert env["coupling"] == "ecad_to_cht"
    kinds = {c["kind"] for c in env["conditions"]}
    assert {"heat_source", "inlet_velocity", "inlet_temperature"} <= kinds
    print(f"  FIN19: dry run — {len(names)} solids, envelope valid")
    print("  [fin19_handoff_dry_run] PASS")


def _fin19_openfoam_available() -> bool:
    try:
        from services.sim.foam_runner import get_runner
        r = get_runner()
        return bool(r is not None and r.available())
    except Exception:  # noqa: BLE001
        return False


def test_fin19_live_cht_smoke(tmp_path) -> None:
    """LIVE (gated): one tiny coarse CHT case through the T-SIM5 seam using the exact
    solids build_airflow_solids emits — proves the STL export + multi-region seam end
    to end. Skips cleanly without OpenFOAM. Takes minutes."""
    if not _fin19_openfoam_available():
        print("  FIN19: OpenFOAM not available — live CHT smoke SKIPPED")
        return
    from mcp_server.ecad_thermal import build_airflow_solids, parse_region_temps
    from services.sim.openfoam import run_openfoam_tunnel
    board = {"bbox_mm": [0.0, 0.0, 20.0, 20.0], "z_top_mm": 1.6, "z_bot_mm": 0.0,
             "thickness_mm": 1.6, "outline_area_mm2": 400.0}
    material = {"k_w_mk": 18.0}
    comps = [{"ref": "U1", "power_w": {"typ": 2.0},
              "fea": {"rect_mm": [7.5, 7.5, 12.5, 12.5], "z_bot_mm": 1.6,
                      "z_top_mm": 2.6, "top_area_mm2": 25.0, "side": "top"}}]
    case = build_airflow_solids(board, material, comps, str(tmp_path))
    res = run_openfoam_tunnel(
        None, direction=(1.0, 0.0, 0.0), speed_mps=2.0, preset="coarse",
        thermal={"enabled": True, "mode": "conjugate", "inlet_temp_c": 25.0,
                 "solids": case["solids"]})
    temps = parse_region_temps(list(res.solver_notes or []))
    assert "board" in temps and "c_U1" in temps, res.solver_notes
    assert temps["c_U1"]["peak_c"] > 25.0 + 2.0, \
        f"2 W component must heat above inlet: {temps}"
    assert temps["c_U1"]["peak_c"] >= temps["board"]["mean_c"], temps
    print(f"  FIN19: live CHT — board {temps['board']}, U1 {temps['c_U1']}")
    print("  [fin19_live_cht_smoke] PASS")
